from __future__ import annotations

import hashlib
import asyncio
import inspect
import json
import zipfile
from datetime import datetime, timedelta, timezone
from dataclasses import replace
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
from agenten.agent_factory.execution_policy import FactoryExecutionMode
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.hermes_cli import InMemoryFactorySkillReplayStore
from agenten.agent_factory.n8n_tools import OpaqueN8nToolReference, TypedN8nTool
from agenten.agent_factory.outcome_contracts import AssertionOutcome, ExecutionOutcomeV1
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
)
from agenten.agent_factory.team_execution import (
    BudgetedChatCompletionClient,
    CaptainN8nGrantAuthority,
    CaptainAuthorizedN8nTool,
    CaptainReleasedSkillAuthority,
    FactoryN8nExecutionEvidenceV1,
    FactoryN8nToolAuthorizationV1,
    FactoryHoldoutAssertionDecisionV1,
    FactoryHoldoutEvaluationReceiptV1,
    FactoryLiveTeamExecutionPorts,
    FactoryPricingQuoteV1,
    FactoryTeamRunResult,
    HostAutoGenTeamRunner,
    ResolvedFactoryHoldoutCase,
    TeamExecutionService,
    _FactoryActivityCeilingTermination,
    _provider_failure_label,
    compose_live_team_execution,
)
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeCommandPayload,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    CapabilityGrantRevocation,
    CapabilityProfile,
    IntegrationIntent,
    RuntimeOperation,
    RuntimeLimits,
    RuntimeStatus,
)
from agenten.targets.n8n import N8nExecutionEvidence
from agenten.agent_runtime.capabilities import PROFILE_CAPABILITIES
from agenten.llm.model_client import build_replay_model_client
from autogen_core.models import FunctionExecutionResult, ModelFamily, ModelInfo, UserMessage
from autogen_core import CancellationToken
from autogen_core.tools import FunctionTool
from autogen_agentchat.messages import ToolCallExecutionEvent
from autogen_ext.models.replay import ReplayChatCompletionClient
from autogen_agentchat.teams import Swarm


NOW = datetime(2026, 7, 21, 13, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_activity_ceiling_excludes_internal_swarm_handoff_tools() -> None:
    termination = _FactoryActivityCeilingTermination(
        max_handoffs=4,
        max_tool_calls=1,
    )
    internal_handoff = ToolCallExecutionEvent(
        source="triage",
        content=[
            FunctionExecutionResult(
                content="handoff",
                name="transfer_to_resolver",
                call_id="handoff-1",
            )
        ],
    )
    executable_tool = ToolCallExecutionEvent(
        source="resolver",
        content=[
            FunctionExecutionResult(
                content="ok",
                name="support_triage",
                call_id="tool-1",
            )
        ],
    )

    assert await termination([internal_handoff]) is None
    stop = await termination([executable_tool])

    assert stop is not None
    assert stop.content == "max_tool_calls"


def test_provider_failure_label_redacts_openai_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPTAIN_RUNTIME_EVIDENCE_DIAGNOSTICS", "1")

    label = _provider_failure_label(
        RuntimeError("Error code: 400 - provider response body omitted")
    )

    assert label == "RuntimeError:openai_status:400"


def test_provider_failure_label_includes_only_safe_openai_error_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProviderError(RuntimeError):
        code = "invalid_value"
        param = "messages.2.role"

    monkeypatch.setenv("CAPTAIN_RUNTIME_EVIDENCE_DIAGNOSTICS", "1")

    label = _provider_failure_label(ProviderError("Error code: 400 - sensitive provider body"))

    assert label == "ProviderError:openai_status:400:code:invalid_value:param:messages.2.role"
    assert "sensitive" not in label


def test_host_runner_binds_outcome_version_to_job_authority() -> None:
    source = inspect.getsource(HostAutoGenTeamRunner.run)

    assert "capability_version=job.subject_version" in source
    assert "team_version=job.subject_version" in source


def _policy_digest(job: AgentFactoryJobV3) -> str:
    encoded = json.dumps(
        job.execution_policy.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _pricing_quote(job: AgentFactoryJobV3) -> FactoryPricingQuoteV1:
    return FactoryPricingQuoteV1(
        quote_id="deterministic-price-v1",
        job_id=job.job_id,
        subject_version=job.subject_version,
        execution_policy_sha256=_policy_digest(job),
        provider="deterministic-replay",
        model="approved-model-id",
        version="2026-07-21",
        effective_at=NOW,
        max_cost_per_call="0.50",
        input_cost_per_million="0",
        output_cost_per_million="0",
        minimum_cost_usd="0.10",
        evidence_ref=_artifact("factory-pricing", "4" * 64),
    )


def _released_skill_fixture(tmp_path: Path) -> tuple[ReleasedHermesSkill, Path]:
    skill_root = tmp_path / "skills"
    directory = skill_root / "captain-factory-execute-team"
    directory.mkdir(parents=True)
    skill_file = directory / "SKILL.md"
    skill_file.write_text("approved host workflow\n", encoding="utf-8")
    entries = [
        {
            "path": "SKILL.md",
            "sha256": hashlib.sha256(skill_file.read_bytes()).hexdigest(),
            "size": skill_file.stat().st_size,
        }
    ]
    digest = hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return (
        ReleasedHermesSkill(
            schema_name="captain.released-hermes-skill.v1",
            skill_id="captain-factory-execute-team",
            version=1,
            capability="factory_workflow",
            content_ref=ArtifactRef(
                uri="artifact://released-skills/captain-factory-execute-team/v1",
                sha256=digest,
                media_type="application/json",
            ),
            content_sha256=digest,
            status="released",
            released_at=NOW,
            producer="captain",
        ),
        skill_root,
    )


class _PaidEffectAuthority:
    def __init__(self) -> None:
        self.calls = 0

    def authorize(self, **kwargs: object) -> ReleasedHermesSkill:
        self.calls += 1
        invocation = kwargs["invocation"]
        assert isinstance(invocation, FactorySkillInvocationV1)
        return invocation.released_skill


class _PricingAuthority:
    def __init__(self, quote: FactoryPricingQuoteV1) -> None:
        self.quote = quote
        self.calls = 0

    def resolve(self, **_: object) -> FactoryPricingQuoteV1:
        self.calls += 1
        return self.quote


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


def _n8n_tool_command_ref(reference: OpaqueN8nToolReference) -> ArtifactRef:
    encoded = json.dumps(
        reference.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _artifact("n8n-command", hashlib.sha256(encoded).hexdigest())


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
    base_job = _job_v3()
    second_holdout = PrivateHoldoutRef(
        schema_name="captain.private-holdout-ref.v1",
        holdout_id="holdout-333333333333",
        uri="holdout://holdout-333333333333",
        sha256="3" * 64,
    )
    third_holdout = PrivateHoldoutRef(
        schema_name="captain.private-holdout-ref.v1",
        holdout_id="holdout-444444444444",
        uri="holdout://holdout-444444444444",
        sha256="4" * 64,
    )
    job = base_job.model_copy(
        update={
            "private_holdout_refs": (
                *base_job.private_holdout_refs,
                second_holdout,
                third_holdout,
            )
        }
    )
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
    second_case = await service.execute(invocation, candidate, second_holdout)
    third_case = await service.execute(invocation, candidate, third_holdout)

    assert evidence.status == "succeeded"
    assert evidence.termination_reason == "task_completed"
    assert evidence.usage_receipt_refs == (_artifact("factory-usage", "8" * 64),)
    assert evidence.handoff_evidence_refs == (handoff_ref,)
    assert evidence.tool_evidence_refs == (tool_ref,)
    assert len(runner.calls) == 3
    assert replayed == evidence
    assert second_case.holdout_ref == second_holdout
    assert third_case.holdout_ref == third_holdout
    assert tuple(
        item.run_number for item in (evidence, second_case, third_case)
    ) == (1, 2, 3)
    assert len(
        {
            item.invocation_id
            for item in (evidence, second_case, third_case)
        }
    ) == 3
    assert len(
        {
            item.invocation.idempotency_key
            for item in (evidence, second_case, third_case)
        }
    ) == 3
    projection = budget.projection(job.job_id)
    assert projection.consumed_usd == Decimal("1.26")
    assert projection.reserved_usd == Decimal("0")


@pytest.mark.asyncio
async def test_budgeted_model_client_reserves_before_every_provider_call(
    tmp_path: Path,
) -> None:
    job = _job_v3()
    budget = InMemoryFactoryBudgetLedger()
    invocation = _invocation(job)
    skill_authority = _PaidEffectAuthority()
    pricing_authority = _PricingAuthority(_pricing_quote(job))
    client = BudgetedChatCompletionClient(
        job=job,
        invocation=invocation,
        attempt=1,
        delegate=build_replay_model_client(["first", "second"]),
        budget=budget,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "provider-evidence"),
        provider="deterministic-replay",
        model="approved-model-id",
        max_cost_per_call=Decimal("0.50"),
        paid_effect_authority=skill_authority,
        pricing_authority=pricing_authority,
        clock=lambda: NOW,
    )

    await client.create([UserMessage(content="one", source="user")])
    await client.create([UserMessage(content="two", source="user")])

    assert len(client.usage_receipts) == 2
    assert client.provider_effect_dispatched_with_unknown_usage is False
    assert skill_authority.calls == 2
    assert pricing_authority.calls == 2
    assert len({item.reservation_id for item in client.usage_receipts}) == 2
    assert {
        item.invocation_id for item in client.usage_receipts
    } == {invocation.invocation_id}
    assert {
        item.lease_id for item in client.usage_receipts
    } == {invocation.lease.lease_id}
    assert {
        getattr(event, "reservation").invocation_id
        for event in budget.events
        if hasattr(event, "reservation")
    } == {invocation.invocation_id}
    assert budget.projection(job.job_id).consumed_usd == Decimal("0.20")
    assert budget.projection(job.job_id).reserved_usd == Decimal("0")


@pytest.mark.asyncio
async def test_dispatched_provider_failure_keeps_unknown_cost_reservation_active(
    tmp_path: Path,
) -> None:
    job = _job_v3()
    budget = InMemoryFactoryBudgetLedger()
    invocation = _invocation(job)
    client = BudgetedChatCompletionClient(
        job=job,
        invocation=invocation,
        attempt=1,
        delegate=build_replay_model_client([]),
        budget=budget,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "unknown-cost"),
        provider="deterministic-replay",
        model="approved-model-id",
        max_cost_per_call=Decimal("0.50"),
        paid_effect_authority=_PaidEffectAuthority(),
        pricing_authority=_PricingAuthority(_pricing_quote(job)),
        clock=lambda: NOW,
    )

    with pytest.raises(Exception):
        await client.create([UserMessage(content="dispatch", source="user")])

    projection = budget.projection(job.job_id)
    assert client.provider_effect_dispatched_with_unknown_usage is True
    assert projection.consumed_usd == Decimal("0")
    assert projection.reserved_usd == Decimal("0.50")


def test_budgeted_model_client_rejects_cross_job_invocation(tmp_path: Path) -> None:
    job = _job_v3()
    other_job = job.model_copy(
        update={
            "job_id": UUID("70000000-0000-0000-0000-000000000099"),
            "correlation_id": UUID("70000000-0000-0000-0000-000000000098"),
        }
    )
    with pytest.raises(ValueError, match="current job"):
        BudgetedChatCompletionClient(
            job=job,
            invocation=_invocation(other_job),
            attempt=1,
            delegate=build_replay_model_client(["unused"]),
            budget=InMemoryFactoryBudgetLedger(),
            evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "cross-job"),
            provider="deterministic-replay",
            model="approved-model-id",
            max_cost_per_call=Decimal("0.50"),
            paid_effect_authority=_PaidEffectAuthority(),
            pricing_authority=_PricingAuthority(_pricing_quote(job)),
            clock=lambda: NOW,
        )


@pytest.mark.asyncio
async def test_cancellation_after_provider_dispatch_keeps_reservation_active(
    tmp_path: Path,
) -> None:
    job = _job_v3()
    started = asyncio.Event()

    class BlockingReplay(ReplayChatCompletionClient):
        async def create(self, *args: object, **kwargs: object) -> object:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    budget = InMemoryFactoryBudgetLedger()
    client = BudgetedChatCompletionClient(
        job=job,
        invocation=_invocation(job),
        attempt=1,
        delegate=BlockingReplay(["unused"]),
        budget=budget,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "cancelled-provider"),
        provider="deterministic-replay",
        model="approved-model-id",
        max_cost_per_call=Decimal("0.50"),
        paid_effect_authority=_PaidEffectAuthority(),
        pricing_authority=_PricingAuthority(_pricing_quote(job)),
        clock=lambda: NOW,
    )
    call = asyncio.create_task(
        client.create([UserMessage(content="dispatch", source="user")])
    )
    await started.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert client.provider_effect_dispatched_with_unknown_usage is True
    assert budget.projection(job.job_id).reserved_usd == Decimal("0.50")


def test_holdout_decision_rejects_truthy_string_false() -> None:
    with pytest.raises(ValueError):
        FactoryHoldoutAssertionDecisionV1(
            assertion_id="real_case_green",
            passed="false",  # type: ignore[arg-type]
            provenance_code="deterministic_rule",
        )


def test_captain_skill_authority_rejects_wrong_capability_and_digest(
    tmp_path: Path,
) -> None:
    job = _job_v3()
    released, skill_root = _released_skill_fixture(tmp_path)

    class Catalog:
        current = released

        def released_for(self, *_: object) -> ReleasedHermesSkill:
            return self.current

    catalog = Catalog()
    authority = CaptainReleasedSkillAuthority(
        catalog=catalog,  # type: ignore[arg-type]
        skill_root=skill_root,
    )
    invocation = _invocation(job).model_copy(update={"released_skill": released})
    assert authority.authorize(job=job, invocation=invocation, now=NOW) == released

    wrong_capability = released.model_copy(update={"capability": "model_invoke"})
    catalog.current = wrong_capability
    with pytest.raises(ValueError, match="not authorized"):
        authority.authorize(
            job=job,
            invocation=invocation.model_copy(
                update={"released_skill": wrong_capability}
            ),
            now=NOW,
        )

    wrong_digest = released.model_copy(
        update={
            "content_ref": ArtifactRef(
                uri="artifact://released-skills/captain-factory-execute-team/v1",
                sha256="f" * 64,
                media_type="application/json",
            ),
            "content_sha256": "f" * 64,
        }
    )
    catalog.current = wrong_digest
    with pytest.raises(ValueError, match="digest"):
        authority.authorize(
            job=job,
            invocation=invocation.model_copy(update={"released_skill": wrong_digest}),
            now=NOW,
        )


def test_embedded_live_composition_is_available_and_fails_closed_without_a_port(
    tmp_path: Path,
) -> None:
    job = _job_v3()
    released, skill_root = _released_skill_fixture(tmp_path)

    class Catalog:
        def released_for(self, *_: object) -> ReleasedHermesSkill:
            return released

    ports = FactoryLiveTeamExecutionPorts(
        model_client_for=lambda *_: build_replay_model_client(["unused"]),
        budget=InMemoryFactoryBudgetLedger(),
        pricing_authority=_PricingAuthority(_pricing_quote(job)),
        replay_store=InMemoryFactorySkillReplayStore(),
        holdouts=object(),  # type: ignore[arg-type]
        n8n_adapter=object(),  # type: ignore[arg-type]
        n8n_authority=object(),  # type: ignore[arg-type]
        released_skill_catalog=Catalog(),  # type: ignore[arg-type]
        skill_root=skill_root,
        tools={},
        provider="deterministic-replay",
        model="approved-model-id",
        max_cost_per_call=Decimal("0.50"),
        clock=lambda: NOW,
    )
    adapter = compose_live_team_execution(
        job=job,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "embedded"),
        ports=ports,
    )
    lease = _invocation(job).lease
    invocation = adapter.invocation_for(
        FactoryDispatch(
            job=job,
            action=FactoryAction(
                kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
                attempt=1,
                job_id=job.job_id,
            ),
            role=FactoryRole.REAL_CASE_TESTER,
            lease=lease,
        )
    )
    assert invocation.step is FactorySkillStep.EXECUTE_TEAM
    assert invocation.released_skill == released
    assert invocation.execution_scope_ref == job.private_holdout_refs[0]

    with pytest.raises(ValueError, match="every authoritative port"):
        compose_live_team_execution(
            job=job,
            evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "closed"),
            ports=replace(ports, n8n_authority=None),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_live_composition_executes_each_explicit_authorized_holdout_scope(
    tmp_path: Path,
) -> None:
    bodies = (
        b"Resolve private release case one.",
        b"Resolve private release case two.",
        b"Resolve private release case three.",
    )
    holdouts = tuple(
        PrivateHoldoutRef(
            schema_name="captain.private-holdout-ref.v1",
            holdout_id=f"holdout-{str(number) * 12}",
            uri=f"holdout://holdout-{str(number) * 12}",
            sha256=hashlib.sha256(body).hexdigest(),
        )
        for number, body in enumerate(bodies, start=1)
    )
    base_job = _job_v3()
    job = base_job.model_copy(
        update={
            "private_holdout_refs": holdouts,
            "execution_policy": base_job.execution_policy.model_copy(
                update={
                    "mode": FactoryExecutionMode.RELEASE,
                    "required_live_runs": 3,
                }
            ),
        }
    )
    candidate = _sealed_team_candidate(tmp_path)
    with zipfile.ZipFile(candidate.source_archive, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    team_payload = json.loads(entries["team_manifest.json"])
    for agent in team_payload["agents"]:
        agent["tools"] = []
    team_bytes = json.dumps(team_payload, sort_keys=True).encode("utf-8")
    entries["team_manifest.json"] = team_bytes
    with zipfile.ZipFile(candidate.source_archive, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    candidate = candidate.model_copy(
        update={
            "candidate": candidate.candidate.model_copy(
                update={
                    "source_archive_ref": candidate.candidate.source_archive_ref.model_copy(
                        update={
                            "sha256": hashlib.sha256(
                                candidate.source_archive.read_bytes()
                            ).hexdigest()
                        }
                    ),
                    "team_manifest": candidate.candidate.team_manifest.model_copy(
                        update={
                            "reference": candidate.candidate.team_manifest.reference.model_copy(
                                update={
                                    "sha256": hashlib.sha256(team_bytes).hexdigest()
                                }
                            )
                        }
                    ),
                }
            )
        }
    )
    released, skill_root = _released_skill_fixture(tmp_path)

    class Catalog:
        def released_for(self, *_: object) -> ReleasedHermesSkill:
            return released

    class Holdouts:
        async def resolve(
            self, reference: PrivateHoldoutRef
        ) -> ResolvedFactoryHoldoutCase:
            return ResolvedFactoryHoldoutCase(
                reference=reference,
                body=bodies[holdouts.index(reference)],
            )

        async def evaluate(
            self,
            reference: PrivateHoldoutRef,
            result: object,
            assertion_ids: tuple[str, ...],
        ) -> FactoryHoldoutEvaluationReceiptV1:
            assert result is not None
            return FactoryHoldoutEvaluationReceiptV1(
                schema_name="captain.factory-holdout-evaluation-receipt.v1",
                holdout_ref=reference,
                candidate_ref=candidate.candidate.source_archive_ref,
                assertion_ids=assertion_ids,
                decisions=tuple(
                    FactoryHoldoutAssertionDecisionV1(
                        assertion_id=assertion_id,
                        passed=True,
                        provenance_code="deterministic_rule",
                    )
                    for assertion_id in assertion_ids
                ),
                evaluator_id="captain_test_evaluator",
                evaluator_version="1",
                evaluated_at=NOW,
            )

    async def support_triage(ticket: str) -> str:
        return f"routed:{ticket}"

    class TrustedN8nAdapter:
        def tool(self, name: str) -> object:
            return FunctionTool(
                support_triage,
                description="Route support case",
                name=name,
            )

        def authorization(self, name: str) -> object:
            raise AssertionError(f"unused n8n tool {name} requested authority")

        def observed_evidence(self) -> tuple[FactoryN8nExecutionEvidenceV1, ...]:
            return ()

    class TrustedN8nAuthority:
        async def authorize_command(self, claim: object, *, now: datetime) -> object:
            raise AssertionError("unused n8n tool requested authority")

        async def authorize(self, evidence: object, *, now: datetime) -> object:
            raise AssertionError("no n8n call should be observed")

    ports = FactoryLiveTeamExecutionPorts(
        model_client_for=lambda *_: ReplayChatCompletionClient(
            ["TERMINATE"],
            model_info=ModelInfo(
                vision=False,
                function_calling=True,
                json_output=True,
                family=ModelFamily.UNKNOWN,
                structured_output=True,
            ),
        ),
        budget=InMemoryFactoryBudgetLedger(),
        pricing_authority=_PricingAuthority(_pricing_quote(job)),
        replay_store=InMemoryFactorySkillReplayStore(),
        holdouts=Holdouts(),  # type: ignore[arg-type]
        n8n_adapter=TrustedN8nAdapter(),  # type: ignore[arg-type]
        n8n_authority=TrustedN8nAuthority(),  # type: ignore[arg-type]
        released_skill_catalog=Catalog(),  # type: ignore[arg-type]
        skill_root=skill_root,
        tools={},
        provider="deterministic-replay",
        model="approved-model-id",
        max_cost_per_call=Decimal("0.50"),
        clock=lambda: NOW,
    )
    adapters = tuple(
        compose_live_team_execution(
            job=job,
            evidence_store=FilesystemFactoryEvidenceStore(
                tmp_path / "composition"
            ),
            ports=ports,
            holdout_selector=lambda _current_job, selected=holdout: selected,
        )
        for holdout in holdouts
    )
    dispatch = FactoryDispatch(
        job=job,
        action=FactoryAction(
            kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
            attempt=1,
            job_id=job.job_id,
        ),
        role=FactoryRole.REAL_CASE_TESTER,
        lease=_invocation(job).lease,
    )

    evidence = tuple(
        [
            await adapter.execute(
                dispatch,
                candidate,
            )
            for adapter in adapters
        ]
    )

    assert tuple(item.run_number for item in evidence) == (1, 2, 3)
    assert len({item.invocation_id for item in evidence}) == 3
    assert len({item.invocation.idempotency_key for item in evidence}) == 3
    assert len({item.artifact_ref for item in evidence}) == 3
    assert all(
        item.execution_outcome.output_ref in item.evidence_refs
        for item in evidence
    )


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
@pytest.mark.parametrize(
    "lease_update",
    [
        {"lease_id": "factory-forged-authority"},
        {"capabilities": ("tests.run",)},
    ],
)
async def test_team_execution_rejects_forged_lease_authority(
    tmp_path: Path,
    lease_update: dict[str, object],
) -> None:
    job = _job_v3()
    invocation = _invocation(job)
    forged = invocation.model_copy(
        update={"lease": invocation.lease.model_copy(update=lease_update)}
    )

    class Runner:
        async def run(self, **_: object) -> FactoryTeamRunResult:
            raise AssertionError("forged lease must fail before the runner")

    with pytest.raises(Exception, match="lease"):
        await TeamExecutionService(
            job=job,
            preflight=_SuccessfulPreflight(),
            runner=Runner(),
            evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "forged-lease"),
            replay_store=InMemoryFactorySkillReplayStore(),
            clock=lambda: NOW,
        ).execute(forged, _candidate(tmp_path), job.private_holdout_refs[0])


@pytest.mark.asyncio
async def test_host_runner_instantiates_autogen_swarm_and_ignores_candidate_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holdout_body = b"Resolve the private support case."
    job = _job_v3(holdout_body=holdout_body)
    sealed_candidate = _sealed_team_candidate(tmp_path)
    budget = InMemoryFactoryBudgetLedger()
    evidence_store = FilesystemFactoryEvidenceStore(tmp_path / "host-evidence")
    invocation = _invocation(job)
    model_client = BudgetedChatCompletionClient(
        job=job,
        invocation=invocation,
        attempt=1,
        delegate=ReplayChatCompletionClient(
            [
                json.dumps(
                    {
                        "schema": "captain.factory-observation.v1",
                        "assertions": [
                            {
                                "assertion_id": "assert-000000000000",
                                "passed": True,
                                "observable": "deterministic replay output",
                            }
                        ],
                        "recovery": {"stop_condition_sha256": "a" * 64},
                        "termination": "TERMINATE",
                    }
                )
            ],
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
        paid_effect_authority=_PaidEffectAuthority(),
        pricing_authority=_PricingAuthority(_pricing_quote(job)),
        clock=lambda: NOW,
    )

    async def support_triage(ticket: str) -> str:
        return f"routed:{ticket}"

    class TrustedN8nAdapter:
        def tool(self, name: str) -> object:
            assert name == "support_triage"
            return FunctionTool(
                support_triage,
                description="Route support case",
                name=name,
            )

        def authorization(self, name: str) -> object:
            raise AssertionError("unused n8n tool must not request authority")

        def observed_evidence(self) -> tuple[FactoryN8nExecutionEvidenceV1, ...]:
            return ()

    class TrustedN8nAuthority:
        async def authorize_command(self, claim: object, *, now: datetime) -> object:
            raise AssertionError("unused n8n tool must not request authority")

        async def authorize(self, evidence: object, *, now: datetime) -> object:
            raise AssertionError("no n8n call should be observed in this run")

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
        ) -> FactoryHoldoutEvaluationReceiptV1:
            assert reference == job.private_holdout_refs[0]
            assert result is not None
            return FactoryHoldoutEvaluationReceiptV1(
                schema_name="captain.factory-holdout-evaluation-receipt.v1",
                holdout_ref=job.private_holdout_refs[0],
                candidate_ref=sealed_candidate.candidate.source_archive_ref,
                assertion_ids=assertion_ids,
                decisions=tuple(
                    FactoryHoldoutAssertionDecisionV1(
                        assertion_id=assertion_id,
                        passed=True,
                        provenance_code="deterministic_rule",
                    )
                    for assertion_id in assertion_ids
                ),
                evaluator_id="captain_test_evaluator",
                evaluator_version="1",
                evaluated_at=NOW,
            )

    untrusted_runner = HostAutoGenTeamRunner(
        model_client=model_client,
        evaluator=FactoryCandidateEvaluator(),
        evidence_store=evidence_store,
        holdouts=Holdouts(),  # type: ignore[arg-type]
        tools={"support_triage": support_triage},
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="trusted n8n adapter and grant authority"):
        await untrusted_runner.run(
            job=job,
            invocation=_invocation(job),
            candidate=sealed_candidate,
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
        n8n_authority=TrustedN8nAuthority(),  # type: ignore[arg-type]
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
        candidate=sealed_candidate,
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
    persisted = [
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "host-evidence").rglob("*.json")
    ]
    observations = [
        json.loads(item)
        for item in persisted
        if json.loads(item).get("schema") == "captain.factory-autogen-observation.v1"
    ]
    assert len(observations) == 1
    observation = observations[0]
    assert observation["invocation_id"] == str(invocation.invocation_id)
    assert observation["session_id"] == f"autogen-team-1-{invocation.invocation_id}"
    assert observation["message_count"] == len(observation["transcript"])
    assert tuple(message["source"] for message in observation["transcript"]) == (
        "user",
        "triage",
    )
    assert all(
        set(message) == {"content_sha256", "message_type", "source"}
        for message in observation["transcript"]
    )
    assert all(
        len(message["content_sha256"]) == 64
        for message in observation["transcript"]
    )
    assert len(observation["transcript_sha256"]) == 64
    assert any("factory-holdout-evaluation-receipt.v1" in item for item in persisted)
    assert all(holdout_body.decode("utf-8") not in item for item in persisted)
    assert all("SENSITIVE-AGENT-OUTPUT" not in item for item in persisted)

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
            candidate=sealed_candidate,
            case_ref=job.private_holdout_refs[0],
            lease=_invocation(job).lease,
            allowed_models=job.execution_policy.allowed_models,
            max_seconds=0.01,
        )
    assert timeout_tokens[0].is_cancelled()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_paid_negative_holdout_is_behavioral_failed_with_usage_receipt(
    tmp_path: Path,
) -> None:
    holdout_body = b"Reject this deterministic private case."
    job = _job_v3(holdout_body=holdout_body)
    candidate = _sealed_team_candidate(tmp_path)
    invocation = _invocation(job)
    model_client = BudgetedChatCompletionClient(
        job=job,
        invocation=invocation,
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
        budget=InMemoryFactoryBudgetLedger(),
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "negative-evidence"),
        provider="deterministic-replay",
        model="approved-model-id",
        max_cost_per_call=Decimal("0.50"),
        paid_effect_authority=_PaidEffectAuthority(),
        pricing_authority=_PricingAuthority(_pricing_quote(job)),
        clock=lambda: NOW,
    )

    async def support_triage(ticket: str) -> str:
        return f"routed:{ticket}"

    class TrustedN8nAdapter:
        def tool(self, name: str) -> object:
            return FunctionTool(
                support_triage,
                description="Route support case",
                name=name,
            )

        def authorization(self, name: str) -> object:
            raise AssertionError(f"unused n8n tool {name} requested authority")

        def observed_evidence(self) -> tuple[FactoryN8nExecutionEvidenceV1, ...]:
            return ()

    class TrustedN8nAuthority:
        async def authorize_command(self, claim: object, *, now: datetime) -> object:
            raise AssertionError("unused n8n tool requested authority")

        async def authorize(self, evidence: object, *, now: datetime) -> object:
            raise AssertionError("no n8n call should be observed")

    class RejectingHoldouts:
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
        ) -> FactoryHoldoutEvaluationReceiptV1:
            assert result is not None
            return FactoryHoldoutEvaluationReceiptV1(
                schema_name="captain.factory-holdout-evaluation-receipt.v1",
                holdout_ref=job.private_holdout_refs[0],
                candidate_ref=candidate.candidate.source_archive_ref,
                assertion_ids=assertion_ids,
                decisions=tuple(
                    FactoryHoldoutAssertionDecisionV1(
                        assertion_id=assertion_id,
                        passed=False,
                        provenance_code="deterministic_rule",
                    )
                    for assertion_id in assertion_ids
                ),
                evaluator_id="captain_test_evaluator",
                evaluator_version="1",
                evaluated_at=NOW,
            )

    result = await HostAutoGenTeamRunner(
        model_client=model_client,
        evaluator=FactoryCandidateEvaluator(),
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "host-negative"),
        holdouts=RejectingHoldouts(),  # type: ignore[arg-type]
        tools={},
        n8n_adapter=TrustedN8nAdapter(),  # type: ignore[arg-type]
        n8n_authority=TrustedN8nAuthority(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    ).run(
        job=job,
        invocation=invocation,
        candidate=candidate,
        case_ref=job.private_holdout_refs[0],
        lease=invocation.lease,
        allowed_models=job.execution_policy.allowed_models,
        max_seconds=10,
    )

    assert result.status == "failed"
    assert result.runtime_result.status is RuntimeStatus.FAILED
    assert result.execution_outcome.status == "failed"
    assert len(result.usage_receipts) == 1


def test_candidate_preflight_preserves_valid_n8n_tool_context(
    tmp_path: Path,
) -> None:
    candidate = _sealed_team_candidate(tmp_path)

    result = FactoryCandidateEvaluator().validate(candidate, max_seconds=10)

    assert result.status == "succeeded"
    assert result.team_execution_manifest is not None
    assert {
        tool
        for agent in result.team_execution_manifest.agents
        for tool in agent.tools
    } == {"support_triage"}


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
        capabilities=tuple(sorted(PROFILE_CAPABILITIES[CapabilityProfile.N8N_BUILDER])),
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
            approved_tool_ref=TypedN8nTool(
                name="support_triage",
                description="Captain-approved support triage",
                input_schema_ref="artifact://factory-support-input",
                output_schema_ref="artifact://factory-support-output",
            ).opaque_reference(),
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
async def test_n8n_authority_rejects_unknown_revoked_and_noncanonical_grants() -> None:
    command_id = UUID("70000000-0000-0000-0000-000000000036")
    job = _job_v3()
    approved_tool_ref = TypedN8nTool(
        name="support_triage",
        description="Captain-approved support triage",
        input_schema_ref="artifact://factory-support-input",
        output_schema_ref="artifact://factory-support-output",
    ).opaque_reference()
    command = AgentRuntimeCommand(
        schema_name="captain.agent-runtime-command.v1",
        event_id=command_id,
        correlation_id=job.correlation_id,
        occurred_at=NOW,
        producer="captain",
        subject_id="support_triage",
        subject_version=1,
        payload=AgentRuntimeCommandPayload(
            operation=RuntimeOperation.CODEX_RUN,
            project_id="factory-team",
            batch_id="factory-team-batch",
            subtask_id="support_triage",
            workspace_ref="workspace://factory/n8n-tool-call",
            prompt_ref=_n8n_tool_command_ref(approved_tool_ref),
            integration_intent=IntegrationIntent.N8N,
            capability_profile=CapabilityProfile.N8N_BUILDER,
            limits=RuntimeLimits(wall_seconds=60, max_iterations=2),
        ),
    )
    grant = CapabilityGrant(
        schema_name="captain.capability-grant.v1",
        grant_id="grant-n8n-team-tool",
        command_id=command_id,
        batch_id="factory-team-batch",
        batch_version=1,
        subtask_id="support_triage",
        workspace_ref="workspace://factory/n8n-tool-call",
        profile=CapabilityProfile.N8N_BUILDER,
        capabilities=tuple(sorted(PROFILE_CAPABILITIES[CapabilityProfile.N8N_BUILDER])),
        mcp_servers=("n8n-mcp",),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    workflow_ref = _artifact("factory-workflow", "a" * 64)
    evidence = FactoryN8nExecutionEvidenceV1(
        tool_name="support_triage",
        approved_tool_ref=approved_tool_ref,
        runtime_command=command,
        capability_grant=grant,
        runtime_result=AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=UUID("70000000-0000-0000-0000-000000000035"),
            command_id=command_id,
            correlation_id=job.correlation_id,
            occurred_at=NOW + timedelta(seconds=1),
            producer="agent-runtime",
            subject_id="support_triage",
            subject_version=1,
            grant_id=grant.grant_id,
            operation=RuntimeOperation.CODEX_RUN,
            status=RuntimeStatus.SUCCEEDED,
        ),
        mcp_call_id="mcp-call-authoritative",
        workflow_ref=workflow_ref,
        execution=N8nExecutionEvidence(
            execution_id="n8n-execution-authoritative",
            workflow_id="workflow-authoritative",
            artifact_digest=workflow_ref.sha256,
            correlation_id=str(job.correlation_id),
            status="success",
        ),
        evidence_ref=_artifact("n8n-execution", "c" * 64),
    )

    class State:
        stored: CapabilityGrant | None = None
        revocation: CapabilityGrantRevocation | None = None

        async def get_grant(self, command_id: UUID) -> CapabilityGrant | None:
            return self.stored

        async def get_grant_revocation(
            self, command_id: UUID
        ) -> CapabilityGrantRevocation | None:
            return self.revocation

    state = State()
    authority = CaptainN8nGrantAuthority(state)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown"):
        await authority.authorize(evidence, now=NOW + timedelta(seconds=1))

    state.stored = grant
    assert await authority.authorize(
        evidence, now=NOW + timedelta(seconds=1)
    ) == grant
    state.revocation = CapabilityGrantRevocation(
        schema_name="captain.capability-grant-revocation.v1",
        revocation_id=uuid4(),
        grant_id=grant.grant_id,
        command_id=command_id,
        revoked_at=NOW + timedelta(milliseconds=500),
        reason="policy_violation",
    )
    underlying_calls = 0

    async def n8n_effect(ticket: str) -> str:
        nonlocal underlying_calls
        underlying_calls += 1
        return ticket

    class Adapter:
        current_grant = grant

        def tool(self, name: str) -> object:
            return FunctionTool(n8n_effect, description="n8n effect", name=name)

        def authorization(self, name: str) -> FactoryN8nToolAuthorizationV1:
            return FactoryN8nToolAuthorizationV1(
                tool_name=name,
                approved_tool_ref=evidence.approved_tool_ref,
                runtime_command=command,
                capability_grant=self.current_grant,
            )

        def observed_evidence(self) -> tuple[FactoryN8nExecutionEvidenceV1, ...]:
            return ()

    adapter = Adapter()
    authorized_tool = CaptainAuthorizedN8nTool(
        name="support_triage",
        approved_tool_ref=evidence.approved_tool_ref,
        adapter=adapter,  # type: ignore[arg-type]
        authority=authority,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    with pytest.raises(Exception, match="revoked"):
        await authority.authorize(evidence, now=NOW + timedelta(seconds=1))
    with pytest.raises(Exception, match="revoked"):
        await authorized_tool.run_json(
            {"ticket": "case-1"},
            CancellationToken(),
        )
    assert underlying_calls == 0

    state.revocation = None
    noncanonical = grant.model_copy(update={"capabilities": ("mcp.n8n",)})
    state.stored = noncanonical
    adapter.current_grant = noncanonical
    forged = evidence.model_copy(update={"capability_grant": noncanonical})
    with pytest.raises(Exception, match="exactly match"):
        await authority.authorize(forged, now=NOW + timedelta(seconds=1))
    with pytest.raises(Exception, match="exactly match"):
        await authorized_tool.run_json(
            {"ticket": "case-2"},
            CancellationToken(),
        )
    assert underlying_calls == 0


@pytest.mark.asyncio
async def test_canonical_tool_a_work_node_cannot_authorize_candidate_tool_b() -> None:
    command_id = UUID("70000000-0000-0000-0000-000000000037")
    job = _job_v3()
    tool_a_ref = TypedN8nTool(
        name="tool_a",
        description="Captain-approved work node",
        input_schema_ref="artifact://tool-a-input",
        output_schema_ref="artifact://tool-a-output",
    ).opaque_reference()
    tool_b_ref = TypedN8nTool(
        name="tool_b",
        description="Candidate-requested work node",
        input_schema_ref="artifact://tool-b-input",
        output_schema_ref="artifact://tool-b-output",
    ).opaque_reference()
    command = AgentRuntimeCommand(
        schema_name="captain.agent-runtime-command.v1",
        event_id=command_id,
        correlation_id=job.correlation_id,
        occurred_at=NOW,
        producer="captain",
        subject_id="tool_a",
        subject_version=1,
        payload=AgentRuntimeCommandPayload(
            operation=RuntimeOperation.CODEX_RUN,
            project_id="factory-team",
            batch_id="factory-team-batch",
            subtask_id="tool_a",
            workspace_ref="workspace://factory/tool-a",
            prompt_ref=_n8n_tool_command_ref(tool_a_ref),
            integration_intent=IntegrationIntent.N8N,
            capability_profile=CapabilityProfile.N8N_BUILDER,
            limits=RuntimeLimits(wall_seconds=60, max_iterations=2),
        ),
    )
    grant = CapabilityGrant(
        schema_name="captain.capability-grant.v1",
        grant_id="grant-n8n-tool-a",
        command_id=command_id,
        batch_id="factory-team-batch",
        batch_version=1,
        subtask_id="tool_a",
        workspace_ref="workspace://factory/tool-a",
        profile=CapabilityProfile.N8N_BUILDER,
        capabilities=tuple(sorted(PROFILE_CAPABILITIES[CapabilityProfile.N8N_BUILDER])),
        mcp_servers=("n8n-mcp",),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )

    class State:
        async def get_grant(self, current_id: UUID) -> CapabilityGrant | None:
            return grant if current_id == command_id else None

        async def get_grant_revocation(
            self, current_id: UUID
        ) -> CapabilityGrantRevocation | None:
            return None

    underlying_tool_b_calls = 0

    async def tool_b_effect(ticket: str) -> str:
        nonlocal underlying_tool_b_calls
        underlying_tool_b_calls += 1
        return ticket

    class Adapter:
        def tool(self, name: str) -> object:
            return FunctionTool(tool_b_effect, description="tool B effect", name=name)

        def authorization(self, name: str) -> FactoryN8nToolAuthorizationV1:
            return FactoryN8nToolAuthorizationV1(
                tool_name="tool_a",
                approved_tool_ref=tool_a_ref,
                runtime_command=command,
                capability_grant=grant,
            )

        def observed_evidence(self) -> tuple[FactoryN8nExecutionEvidenceV1, ...]:
            return ()

    authorized_tool = CaptainAuthorizedN8nTool(
        name="tool_b",
        approved_tool_ref=tool_b_ref,
        adapter=Adapter(),  # type: ignore[arg-type]
        authority=CaptainN8nGrantAuthority(State()),  # type: ignore[arg-type]
        clock=lambda: NOW + timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="different tool|work node"):
        await authorized_tool.run_json({"ticket": "case-b"}, CancellationToken())
    assert underlying_tool_b_calls == 0

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
async def test_pre_effect_host_configuration_failure_abandons_replay_claim(
    tmp_path: Path,
) -> None:
    job = _job_v3()

    class PreEffectHost(HostAutoGenTeamRunner):
        def __init__(self) -> None:
            self.calls = 0

        @property
        def paid_effect_started(self) -> bool:
            return False

        @property
        def provider_effect_dispatched_with_unknown_usage(self) -> bool:
            return False

        async def run(self, **_: object) -> FactoryTeamRunResult:
            self.calls += 1
            raise ValueError("holdout resolver is not configured")

    runner = PreEffectHost()
    service = TeamExecutionService(
        job=job,
        preflight=_SuccessfulPreflight(),
        runner=runner,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "pre-effect"),
        replay_store=InMemoryFactorySkillReplayStore(),
        clock=lambda: NOW,
    )
    for _ in range(2):
        with pytest.raises(ValueError, match="holdout resolver"):
            await service.execute(
                _invocation(job),
                _candidate(tmp_path),
                job.private_holdout_refs[0],
            )
    assert runner.calls == 2


@pytest.mark.asyncio
async def test_pre_dispatch_caller_cancellation_abandons_replay_for_retry(
    tmp_path: Path,
) -> None:
    job = _job_v3()
    started = asyncio.Event()

    class Runner(HostAutoGenTeamRunner):
        max_cost_usd = Decimal("1.00")

        def __init__(self) -> None:
            self.calls = 0

        @property
        def provider_effect_dispatched_with_unknown_usage(self) -> bool:
            return False

        async def run(self, **_: object) -> FactoryTeamRunResult:
            self.calls += 1
            started.set()
            if self.calls == 1:
                await asyncio.Event().wait()
            raise ValueError("pre-dispatch configuration failure")

    runner = Runner()
    service = TeamExecutionService(
        job=job,
        preflight=_SuccessfulPreflight(),
        runner=runner,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
        replay_store=InMemoryFactorySkillReplayStore(),
        clock=lambda: NOW,
    )
    execution = asyncio.create_task(
        service.execute(_invocation(job), _candidate(tmp_path), job.private_holdout_refs[0])
    )
    await started.wait()
    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution
    with pytest.raises(ValueError, match="pre-dispatch"):
        await service.execute(
            _invocation(job), _candidate(tmp_path), job.private_holdout_refs[0]
        )
    assert runner.calls == 2


@pytest.mark.asyncio
async def test_post_dispatch_cancellation_completes_unresolved_replay(
    tmp_path: Path,
) -> None:
    job = _job_v3()
    started = asyncio.Event()

    class BlockingReplay(ReplayChatCompletionClient):
        async def create(self, *args: object, **kwargs: object) -> object:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    budget = InMemoryFactoryBudgetLedger()
    model_client = BudgetedChatCompletionClient(
        job=job,
        invocation=_invocation(job),
        attempt=1,
        delegate=BlockingReplay(["unused"]),
        budget=budget,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "provider-evidence"),
        provider="deterministic-replay",
        model="approved-model-id",
        max_cost_per_call=Decimal("0.50"),
        paid_effect_authority=_PaidEffectAuthority(),
        pricing_authority=_PricingAuthority(_pricing_quote(job)),
        clock=lambda: NOW,
    )

    class PostDispatchHost(HostAutoGenTeamRunner):
        def __init__(self) -> None:
            self._model_client = model_client
            self.calls = 0

        async def run(self, **_: object) -> FactoryTeamRunResult:
            self.calls += 1
            await self._model_client.create(
                [UserMessage(content="dispatch", source="user")]
            )
            raise AssertionError("unreachable")

    runner = PostDispatchHost()
    service = TeamExecutionService(
        job=job,
        preflight=_SuccessfulPreflight(),
        runner=runner,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
        replay_store=InMemoryFactorySkillReplayStore(),
        clock=lambda: NOW,
    )
    invocation = _invocation(job)
    execution = asyncio.create_task(
        service.execute(invocation, _candidate(tmp_path), job.private_holdout_refs[0])
    )
    await started.wait()
    execution.cancel()

    evidence = await execution
    replayed = await service.execute(
        invocation, _candidate(tmp_path), job.private_holdout_refs[0]
    )

    assert evidence.status == "unresolved"
    assert evidence.termination_reason == "provider_cost_unresolved"
    assert replayed == evidence
    assert runner.calls == 1
    assert model_client.provider_effect_dispatched_with_unknown_usage is True
    assert budget.projection(job.job_id).reserved_usd == Decimal("0.50")


@pytest.mark.asyncio
async def test_cancellation_after_recorded_usage_completes_unresolved_replay(
    tmp_path: Path,
) -> None:
    job = _job_v3()
    usage_recorded = asyncio.Event()
    budget = InMemoryFactoryBudgetLedger()
    model_client = BudgetedChatCompletionClient(
        job=job,
        invocation=_invocation(job),
        attempt=1,
        delegate=build_replay_model_client(["provider-complete"]),
        budget=budget,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "provider-evidence"),
        provider="deterministic-replay",
        model="approved-model-id",
        max_cost_per_call=Decimal("0.50"),
        paid_effect_authority=_PaidEffectAuthority(),
        pricing_authority=_PricingAuthority(_pricing_quote(job)),
        clock=lambda: NOW,
    )

    class RecordedUsageHost(HostAutoGenTeamRunner):
        def __init__(self) -> None:
            self._model_client = model_client
            self.calls = 0

        async def run(self, **_: object) -> FactoryTeamRunResult:
            self.calls += 1
            await self._model_client.create(
                [UserMessage(content="dispatch", source="user")]
            )
            usage_recorded.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    runner = RecordedUsageHost()
    service = TeamExecutionService(
        job=job,
        preflight=_SuccessfulPreflight(),
        runner=runner,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
        replay_store=InMemoryFactorySkillReplayStore(),
        clock=lambda: NOW,
    )
    invocation = _invocation(job)
    execution = asyncio.create_task(
        service.execute(invocation, _candidate(tmp_path), job.private_holdout_refs[0])
    )
    await usage_recorded.wait()
    execution.cancel()

    evidence = await execution
    replayed = await service.execute(
        invocation, _candidate(tmp_path), job.private_holdout_refs[0]
    )

    assert evidence.status == "unresolved"
    assert evidence.termination_reason == "provider_cost_unresolved"
    assert evidence.usage_receipt_refs == tuple(
        receipt.evidence_ref for receipt in model_client.usage_receipts
    )
    assert replayed == evidence
    assert runner.calls == 1
    assert model_client.any_provider_effect_started is True
    assert model_client.provider_effect_dispatched_with_unknown_usage is False
    projection = budget.projection(job.job_id)
    assert projection.consumed_usd == Decimal("0.10")
    assert projection.reserved_usd == Decimal("0")

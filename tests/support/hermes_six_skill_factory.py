"""Deterministic ports for the production six-skill Factory coordinator.

This module deliberately owns no lifecycle policy.  Captain transitions are
derived and written only by ``FactoryCoordinator`` and
``FactorySixSkillLiveCoordinator``.  The fakes below stop at process/provider
boundaries and expose their calls for integration assertions.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from autogen_core import FunctionCall
from autogen_core.models import CreateResult, ModelFamily, ModelInfo, RequestUsage
from autogen_core.tools import FunctionTool
from autogen_ext.models.replay import ReplayChatCompletionClient

from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.execution_budget import (
    BudgetExhausted,
    FactoryBudgetProjection,
    InMemoryFactoryBudgetLedger,
)
from agenten.agent_factory.factory_feedback import FactoryFeedbackBuilder
from agenten.agent_factory.factory_live_runner import (
    FactoryInfrastructureFailure,
    FactoryLiveEffectKind,
    FactoryLiveEffectOutcomeV1,
    FactoryLiveEffectRequestV1,
    FactoryLiveRunReport,
    FactoryLiveRunner,
    InMemoryFactoryLiveEffectLedger,
)
from agenten.agent_factory.hermes_cli import (
    FilesystemFactorySkillReplayStore,
    HermesCliFactory,
    HermesCliSettings,
    _factory_invocation,
)
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.live_composition import (
    FactoryLiveRuntimeComponents,
    FactoryLiveRuntimePorts,
    compose_live_factory_runtime,
)
from agenten.agent_factory.live_n8n import ScopedCaptainN8nMcpAdapter
from agenten.agent_factory.live_pricing import _execution_policy_digest
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatcher
from agenten.agent_factory.service import (
    FactoryCoordinator,
    FactoryLifecycleError,
    InMemoryFactoryRepository,
)
from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_factory.skill_workflow_contracts import (
    CodebaseInventoryV1,
    CodexBuildBriefV1,
    FactoryFeedbackRecommendation,
    FactoryFeedbackV1,
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.skill_sequence import SkillSequencePolicy
from agenten.agent_factory.state_machine import (
    FactoryActionKind,
    FactoryProjection,
)
from agenten.agent_factory.team_execution import (
    FactoryHoldoutAssertionDecisionV1,
    FactoryHoldoutEvaluationReceiptV1,
    FactoryN8nExecutionEvidenceV1,
    FactoryN8nToolAuthorizationV1,
    FactoryPricingQuoteV1,
    ResolvedFactoryHoldoutCase,
    _run_scoped_invocation,
)
from agenten.agent_runtime.capabilities import PROFILE_CAPABILITIES, validate_grant
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeCommandPayload,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    CapabilityProfile,
    IntegrationIntent,
    RuntimeLimits,
    RuntimeOperation,
    RuntimeStatus,
)
from agenten.targets.n8n import N8nExecutionEvidence
from tests.agent_factory.test_factory_live_runner import effect_outcome
from tests.agent_factory.test_hermes_cli import (
    _catalog_for,
    _improvement_authorization,
    _invocation_from_prompt,
)
from tests.agent_factory.test_release_gate import (
    workflow_evaluation,
    workflow_job,
)
from tests.agent_factory.test_team_execution import _sealed_team_candidate
from tests.agent_factory.test_skill_workflow_contracts import (
    brief_payload,
    feedback_payload,
    inventory_payload,
    invocation_payload,
    revision_payload,
    tool_gap_payload,
)


NOW = datetime(2026, 7, 21, 10, 5, tzinfo=timezone.utc)
FIRST_PASS_STEPS = (
    "discover",
    "brief_codex",
    "execute_team",
    "evaluate_team",
    "report_captain",
)
RETRY_STEPS = (
    *FIRST_PASS_STEPS,
    "discover",
    "improve_team",
    "brief_codex",
    "execute_team",
    "evaluate_team",
    "report_captain",
)


def _persist_state(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))
    temporary.replace(path)


def _read_state(path: Path) -> object:
    return pickle.loads(path.read_bytes())


class PersistentFactoryBudgetLedger(InMemoryFactoryBudgetLedger):
    """Reload exact budget events for every newly constructed component."""

    def __init__(self, state_path: Path) -> None:
        super().__init__()
        self._state_path = state_path
        self._reload()

    def _reload(self) -> None:
        if self._state_path.is_file():
            self._events = _read_state(self._state_path)  # type: ignore[assignment]

    def _persist(self) -> None:
        _persist_state(self._state_path, self._events)

    @property
    def events(self):
        self._reload()
        return super().events

    def reserve(self, *args, **kwargs):
        self._reload()
        reservation = super().reserve(*args, **kwargs)
        self._persist()
        return reservation

    def record_usage(self, *args, **kwargs):
        self._reload()
        receipt = super().record_usage(*args, **kwargs)
        self._persist()
        return receipt

    def release(self, *args, **kwargs):
        self._reload()
        receipt = super().release(*args, **kwargs)
        self._persist()
        return receipt

    def projection(self, job_id: UUID) -> FactoryBudgetProjection:
        self._reload()
        return super().projection(job_id)


class WorkflowGatewayRepository(InMemoryFactoryRepository):
    """File-rehydrated Gateway port with the production repository read surface."""

    def __init__(
        self,
        job: AgentFactoryJobV3,
        budget: PersistentFactoryBudgetLedger,
        state_path: Path,
    ) -> None:
        super().__init__()
        self._state_path = state_path
        self.artifacts: list[object] = []
        self.budget = budget
        if self._state_path.is_file():
            self._reload()
            if self.job(job.job_id) != job:
                raise ValueError("persisted Factory job does not match restart input")
        else:
            super().register(job)
            self._persist()

    def _reload(self) -> None:
        if not self._state_path.is_file():
            return
        state = _read_state(self._state_path)
        (
            self._jobs,
            self._blocks,
            self._event_ids,
            self._evaluations_by_job,
            self._release_decisions_by_job,
            self.artifacts,
        ) = state  # type: ignore[misc]

    def _persist(self) -> None:
        _persist_state(
            self._state_path,
            (
                self._jobs,
                self._blocks,
                self._event_ids,
                self._evaluations_by_job,
                self._release_decisions_by_job,
                self.artifacts,
            ),
        )

    def job(self, job_id: UUID):
        self._reload()
        return super().job(job_id)

    def append(self, block: FactoryEvidenceBlock) -> bool:
        self._reload()
        appended = super().append(block)
        self._persist()
        return appended

    def blocks(self, job_id: UUID) -> tuple[FactoryEvidenceBlock, ...]:
        self._reload()
        return super().blocks(job_id)

    def append_artifact(self, artifact: object) -> None:
        self._reload()
        self.artifacts.append(artifact)
        self._persist()

    def extend_artifacts(self, artifacts: tuple[object, ...]) -> None:
        self._reload()
        self.artifacts.extend(artifacts)
        self._persist()

    def workflow_artifacts(self, job_id: UUID) -> tuple[object, ...]:
        self._reload()
        self.job(job_id)
        return tuple(self.artifacts)

    def workflow_budget_projection(self, job_id: UUID) -> FactoryBudgetProjection:
        self.job(job_id)
        return self.budget.projection(job_id)

    def workflow_usage_receipts(self, job_id: UUID) -> tuple[object, ...]:
        self.job(job_id)
        return tuple(
            event.receipt
            for event in self.budget.events
            if hasattr(event, "receipt") and event.receipt.job_id == job_id
        )



class DeterministicClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def __call__(self) -> datetime:
        return self.value


class DeterministicLeases:
    def __init__(self, clock: DeterministicClock) -> None:
        self._clock = clock

    def active(
        self,
        job: AgentFactoryJobV3,
        role: FactoryRole,
        attempt: int,
        now: datetime,
    ):
        assert now == self._clock()
        return issue_factory_lease(
            job=job,
            role=role,
            attempt=attempt,
            workspace_ref="workspace://factory/deterministic-integration",
            now=now,
        )


class DeterministicImprovements:
    def active(self, job, action, projection, now):
        del job, action, projection, now
        return _improvement_authorization()


class GatewayLifecycleView:
    """Read/write facade over the real, replaceable Gateway coordinator."""

    def __init__(self, coordinator: FactoryCoordinator) -> None:
        self._coordinator = coordinator

    def replace(self, coordinator: FactoryCoordinator) -> None:
        self._coordinator = coordinator

    def next_action(self, job_id: UUID):
        return self._coordinator.next_action(job_id)

    def projection(self, job_id: UUID) -> FactoryProjection:
        return self._coordinator.projection(job_id)

    def record(self, block: FactoryEvidenceBlock) -> bool:
        return self._coordinator.record(block)

    def promotion_block(self, job_id: UUID) -> FactoryEvidenceBlock | None:
        return next(
            (
                block
                for block in reversed(self._coordinator.blocks(job_id))
                if block.phase is FactoryPhase.CAPABILITY_PROMOTED
            ),
            None,
        )


class DeterministicCodexBoundary:
    """External Codex process boundary used by the composed build adapter."""

    async def start(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def resume(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def status(self, command, grant):
        return self._result(command, grant)

    async def cancel(self, command, grant):
        return self._result(command, grant)

    async def heartbeat(self, command, grant):
        return self._result(command, grant)

    @staticmethod
    def _result(
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        reference = ArtifactRef(
            uri=f"artifact://deterministic/codex/{command.event_id}",
            sha256=hashlib.sha256(str(command.event_id).encode()).hexdigest(),
            media_type="application/json",
        )
        return AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=uuid5(NAMESPACE_URL, f"codex-result:{command.event_id}"),
            command_id=command.event_id,
            correlation_id=command.correlation_id,
            occurred_at=NOW + timedelta(seconds=1),
            producer="agent-runtime",
            subject_id=command.subject_id,
            subject_version=command.subject_version,
            grant_id=grant.grant_id,
            operation=command.payload.operation,
            status=RuntimeStatus.SUCCEEDED,
            session_id=f"codex-{str(command.event_id)[:12]}",
            artifact_refs=(reference,),
            evidence_refs=(reference,),
        )


class DeterministicCandidateProvider:
    """External Forge archive lookup boundary."""

    def __init__(self, candidate) -> None:
        self._candidate = candidate

    def candidate_for(self, job):
        del job
        return self._candidate


def _without_candidate_tools(candidate):
    with zipfile.ZipFile(candidate.source_archive, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(entries["team_manifest.json"])
    for agent in manifest["agents"]:
        agent["tools"] = []
    manifest_body = json.dumps(manifest, sort_keys=True).encode()
    entries["team_manifest.json"] = manifest_body
    with zipfile.ZipFile(candidate.source_archive, "w") as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    updated_manifest = candidate.candidate.team_manifest.model_copy(
        update={
            "reference": candidate.candidate.team_manifest.reference.model_copy(
                update={"sha256": hashlib.sha256(manifest_body).hexdigest()}
            )
        }
    )
    updated_candidate = candidate.candidate.model_copy(
        update={
            "source_archive_ref": candidate.candidate.source_archive_ref.model_copy(
                update={
                    "sha256": hashlib.sha256(
                        candidate.source_archive.read_bytes()
                    ).hexdigest()
                }
            ),
            "team_manifest": updated_manifest,
        }
    )
    return candidate.model_copy(update={"candidate": updated_candidate})


class DeterministicN8nAuthority:
    async def authorize_command(self, claim, *, now: datetime):
        return validate_grant(
            claim.capability_grant,
            claim.runtime_command,
            now,
        )

    async def authorize(self, evidence, *, now: datetime):
        return validate_grant(
            evidence.capability_grant,
            evidence.runtime_command,
            now,
        )


class DeterministicN8nBoundary:
    """One typed MCP boundary with a fresh Captain claim per provider run."""

    def __init__(self, harness: "SixSkillFactoryHarness", workspace_ref: str) -> None:
        self._harness = harness
        self._workspace_ref = workspace_ref
        self._tool_ref = (
            harness.sealed_candidate.candidate.n8n_tools[0].opaque_reference()
        )
        self._active_claim: FactoryN8nToolAuthorizationV1 | None = None
        self._evidence: list[FactoryN8nExecutionEvidenceV1] = []

    def tool(self, name: str):
        assert name == "support_triage"

        async def support_triage(ticket: str) -> str:
            claim = self._active_claim
            if claim is None:
                raise AssertionError("n8n call started without a Captain claim")
            self._harness.effect_order.append("n8n:start")
            sequence = len(self._evidence) + 1
            workflow_ref = ArtifactRef(
                uri=f"artifact://deterministic/n8n/workflow/{sequence}",
                sha256=hashlib.sha256(
                    f"workflow:{claim.runtime_command.event_id}".encode()
                ).hexdigest(),
                media_type="application/json",
            )
            runtime = AgentRuntimeResult(
                schema_name="captain.agent-runtime-result.v1",
                event_id=uuid5(
                    NAMESPACE_URL,
                    f"n8n-runtime:{claim.runtime_command.event_id}",
                ),
                command_id=claim.runtime_command.event_id,
                correlation_id=claim.runtime_command.correlation_id,
                occurred_at=NOW + timedelta(seconds=1),
                producer="agent-runtime",
                subject_id=claim.runtime_command.subject_id,
                subject_version=claim.runtime_command.subject_version,
                grant_id=claim.capability_grant.grant_id,
                operation=claim.runtime_command.payload.operation,
                status=RuntimeStatus.SUCCEEDED,
            )
            observed = FactoryN8nExecutionEvidenceV1(
                tool_name=name,
                approved_tool_ref=self._tool_ref,
                runtime_command=claim.runtime_command,
                capability_grant=claim.capability_grant,
                runtime_result=runtime,
                mcp_call_id=f"mcp-call-{sequence}",
                workflow_ref=workflow_ref,
                execution=N8nExecutionEvidence(
                    execution_id=f"n8n-execution-{sequence}",
                    workflow_id=f"workflow-{sequence}",
                    artifact_digest=workflow_ref.sha256,
                    correlation_id=str(claim.runtime_command.correlation_id),
                    status="success",
                ),
                evidence_ref=ArtifactRef(
                    uri=f"artifact://deterministic/n8n/execution/{sequence}",
                    sha256=hashlib.sha256(
                        f"execution:{claim.runtime_command.event_id}".encode()
                    ).hexdigest(),
                    media_type="application/json",
                ),
            )
            self._evidence.append(observed)
            self._harness.effect_order.append("n8n:evidence")
            self._harness.effect_counts["n8n"] += 1
            return f"routed:{ticket}"

        return FunctionTool(
            support_triage,
            description="Route one Captain-authorized support case",
            name=name,
        )

    def authorization(self, name: str) -> FactoryN8nToolAuthorizationV1:
        assert name == "support_triage"
        self._harness.effect_order.append("n8n:claim")
        sequence = len(self._evidence) + 1
        command_id = uuid5(
            NAMESPACE_URL,
            f"n8n-command:{self._harness.job.job_id}:{sequence}",
        )
        encoded = json.dumps(
            self._tool_ref.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        command = AgentRuntimeCommand(
            schema_name="captain.agent-runtime-command.v1",
            event_id=command_id,
            correlation_id=self._harness.job.correlation_id,
            occurred_at=NOW,
            producer="captain",
            subject_id=name,
            subject_version=self._harness.job.subject_version,
            payload=AgentRuntimeCommandPayload(
                operation=RuntimeOperation.CODEX_RUN,
                project_id="factory-team",
                batch_id="factory-n8n-batch",
                subtask_id=name,
                workspace_ref=self._workspace_ref,
                prompt_ref=ArtifactRef(
                    uri=f"artifact://deterministic/n8n/command/{sequence}",
                    sha256=hashlib.sha256(encoded).hexdigest(),
                    media_type="application/json",
                ),
                integration_intent=IntegrationIntent.N8N,
                capability_profile=CapabilityProfile.N8N_BUILDER,
                limits=RuntimeLimits(wall_seconds=60, max_iterations=2),
            ),
        )
        grant = CapabilityGrant(
            schema_name="captain.capability-grant.v1",
            grant_id=f"grant-n8n-{sequence}",
            command_id=command_id,
            batch_id="factory-n8n-batch",
            batch_version=self._harness.job.subject_version,
            subtask_id=name,
            workspace_ref=self._workspace_ref,
            profile=CapabilityProfile.N8N_BUILDER,
            capabilities=tuple(
                sorted(PROFILE_CAPABILITIES[CapabilityProfile.N8N_BUILDER])
            ),
            mcp_servers=("n8n-mcp",),
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        self._active_claim = FactoryN8nToolAuthorizationV1(
            tool_name=name,
            approved_tool_ref=self._tool_ref,
            runtime_command=command,
            capability_grant=grant,
        )
        return self._active_claim

    def observed_evidence(self) -> tuple[FactoryN8nExecutionEvidenceV1, ...]:
        return tuple(self._evidence[-1:])

    @property
    def all_evidence(self) -> tuple[FactoryN8nExecutionEvidenceV1, ...]:
        return tuple(self._evidence)


class DeterministicPricingBoundary:
    def __init__(self, job: AgentFactoryJobV3) -> None:
        self._quote = FactoryPricingQuoteV1(
            quote_id="deterministic-price-v1",
            job_id=job.job_id,
            subject_version=job.subject_version,
            execution_policy_sha256=_execution_policy_digest(job),
            provider="deterministic-provider",
            model="approved-model-id",
            version="2026-07-21",
            effective_at=NOW,
            max_cost_per_call="0.25",
            input_cost_per_million="0",
            output_cost_per_million="0",
            minimum_cost_usd="0.25",
            evidence_ref=ArtifactRef(
                uri="artifact://deterministic/pricing/v1",
                sha256=hashlib.sha256(b"deterministic-price-v1").hexdigest(),
                media_type="application/json",
            ),
        )

    def resolve_quote(self, **kwargs):
        assert kwargs["job"].job_id == self._quote.job_id
        return self._quote


class DeterministicHoldoutSource:
    def __init__(self, bodies: dict[PrivateHoldoutRef, bytes]) -> None:
        self._bodies = bodies

    async def read(self, reference):
        return self._bodies[reference]


class DeterministicHoldoutEvaluator:
    def __init__(self, harness: "SixSkillFactoryHarness") -> None:
        self._harness = harness

    async def evaluate(self, reference, result, assertion_ids):
        assert result is not None
        attempt = self._harness._team_attempt_by_holdout[reference]
        passed = not self._harness.team_run_should_fail(attempt)
        return FactoryHoldoutEvaluationReceiptV1(
            schema_name="captain.factory-holdout-evaluation-receipt.v1",
            holdout_ref=reference,
            candidate_ref=self._harness.sealed_candidate.candidate.source_archive_ref,
            assertion_ids=assertion_ids,
            decisions=tuple(
                FactoryHoldoutAssertionDecisionV1(
                    assertion_id=assertion_id,
                    passed=passed,
                    provenance_code="deterministic_rule",
                )
                for assertion_id in assertion_ids
            ),
            evaluator_id="captain_test_evaluator",
            evaluator_version="1",
            evaluated_at=NOW,
        )


class CountingReplayModelClient(ReplayChatCompletionClient):
    def __init__(self, harness: "SixSkillFactoryHarness", responses) -> None:
        super().__init__(
            responses,
            model_info=ModelInfo(
                vision=False,
                function_calling=True,
                json_output=True,
                family=ModelFamily.UNKNOWN,
                structured_output=True,
            ),
        )
        self._harness = harness

    async def create(self, *args, **kwargs):
        self._harness.team_execution_provider_calls += 1
        return await super().create(*args, **kwargs)


class ProductionTeamExecutionRouter:
    """Route run scopes to real composed TeamExecutionCandidateAdapters."""

    def __init__(self, adapter: object, required_runs: int) -> None:
        self._adapter = adapter
        self._required_runs = required_runs
        self._claimed: dict[UUID, TeamExecutionEvidenceV1] = {}
        self._run_by_invocation: dict[UUID, int] = {}
        self.execute_calls = 0

    def invocation_for_run(
        self,
        request: FactoryDispatch,
        run_number: int,
    ) -> FactorySkillInvocationV1:
        base = self._adapter.invocation_for(request)
        holdout = request.job.private_holdout_refs[0]
        invocation = _run_scoped_invocation(
            base,
            holdout,
            run_number,
            required_live_runs=self._required_runs,
        )
        self._run_by_invocation[invocation.invocation_id] = run_number
        return invocation

    async def execute_claimed(
        self,
        request: FactoryDispatch,
        candidate,
        invocation: FactorySkillInvocationV1,
    ) -> TeamExecutionEvidenceV1:
        run_number = self._run_by_invocation[invocation.invocation_id]
        try:
            evidence = await self._adapter.execute_run(
                request,
                candidate,
                run_number=run_number,
            )
        except RuntimeError as exc:
            if "BudgetExhausted: factory USD budget is exhausted" not in str(exc):
                raise
            raise BudgetExhausted("factory USD budget is exhausted") from exc
        if evidence.invocation != invocation:
            raise AssertionError("claimed team invocation changed before execution")
        self._claimed[invocation.invocation_id] = evidence
        return evidence

    async def execute(
        self,
        request: FactoryDispatch,
        candidate,
    ) -> TeamExecutionEvidenceV1:
        run_number = self.execute_calls % self._required_runs + 1
        evidence = await self._adapter.execute_run(
            request,
            candidate,
            run_number=run_number,
        )
        claimed = self._claimed[evidence.invocation_id]
        if evidence != claimed:
            raise AssertionError("team execution replay changed durable evidence")
        self.execute_calls += 1
        return evidence


class DeterministicForgeBoundary:
    def __init__(self, harness: "SixSkillFactoryHarness") -> None:
        self._harness = harness

    async def submit(self, request: FactoryDispatch) -> object:
        self._harness.coordinator.record(
            self._harness.external_block(
                FactoryPhase.AGENT_CODE_CREATED,
                attempt=request.action.attempt,
                producer="hermes",
            )
        )
        return SimpleNamespace(job_id=request.job.job_id)


class DeterministicCandidateBoundary:
    """Fake only the build/provider boundary; FactoryDispatcher owns persistence."""

    def __init__(self, harness: "SixSkillFactoryHarness") -> None:
        self._harness = harness

    async def dispatch(self, request: FactoryDispatch) -> FactoryEvidenceBlock:
        if request.action.kind is FactoryActionKind.DISPATCH_BUILD_VALIDATOR:
            return self._harness.external_block(
                FactoryPhase.BUILD_PASSED,
                attempt=request.action.attempt,
                producer="hermes",
            )
        if request.action.kind is FactoryActionKind.DISPATCH_REAL_CASE_TESTER:
            candidate = self._harness.components.candidate_provider.candidate_for(
                request.job
            )
            assert candidate.source_archive.is_file()
            executions = tuple(
                [
                    await self._harness.components.team_execution.execute(
                        request,
                        candidate,
                    )
                    for _ in range(
                        request.job.execution_policy.required_live_runs
                    )
                ]
            )
            self._harness._attempt_runs[request.action.attempt] = executions
            self._harness.repository.extend_artifacts(executions)
            return self._harness.external_block(
                FactoryPhase.REAL_CASE_EVIDENCE,
                attempt=request.action.attempt,
                producer="hermes",
                role=FactoryRole.REAL_CASE_TESTER,
                artifact_refs=tuple(item.artifact_ref for item in executions),
                evidence_refs=tuple(
                    reference
                    for item in executions
                    for reference in item.evidence_refs
                ),
                assertion_ids=self._harness.job.acceptance_assertion_ids,
            )
        if request.action.kind is FactoryActionKind.DISPATCH_QUALITY_WARDEN:
            return await self._harness.components.hermes.dispatch(request)
        raise AssertionError(
            f"candidate boundary received {request.action.kind.value}"
        )


class DeterministicHermes(HermesCliFactory):
    """Real Hermes adapter with only its subprocess boundary replaced."""

    def __init__(self, harness: "SixSkillFactoryHarness", **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._harness = harness

    async def _run_skill_prompt(self, prompt: str, *, max_seconds: float) -> bytes:
        assert max_seconds > 0
        self._harness.process_calls += 1
        invocation = FactorySkillInvocationV1.model_validate(
            _invocation_from_prompt(prompt)
        )
        artifact = self._harness.artifact_for(invocation)
        self._harness.repository.append_artifact(artifact)
        return artifact.model_dump_json(by_alias=True).encode("utf-8")


class DeterministicDispatcher:
    """External-boundary driver; every block is still validated by Captain."""

    def __init__(
        self,
        harness: "SixSkillFactoryHarness",
        dispatcher: FactoryDispatcher,
    ) -> None:
        self._harness = harness
        self._dispatcher = dispatcher
        self.dispatch_count = 0
        self.actions: list[str] = []

    def replace(self, dispatcher: FactoryDispatcher) -> None:
        self._dispatcher = dispatcher

    def validate_next(
        self,
        job: AgentFactoryJobV3,
        action,
        expected_skill_digests,
    ):
        assert expected_skill_digests
        role = {
            FactoryActionKind.DISPATCH_AGENT_ARCHITECT: FactoryRole.AGENT_ARCHITECT,
            FactoryActionKind.DISPATCH_TOOL_INTEGRATOR: FactoryRole.TOOL_INTEGRATOR,
            FactoryActionKind.DISPATCH_REAL_CASE_TESTER: FactoryRole.REAL_CASE_TESTER,
            FactoryActionKind.DISPATCH_QUALITY_WARDEN: FactoryRole.QUALITY_WARDEN,
        }.get(action.kind)
        if role is not None:
            lease = self._harness.leases.active(
                job,
                role,
                action.attempt,
                self._harness.clock(),
            )
            improvement = None
            if role is FactoryRole.TOOL_INTEGRATOR and action.attempt > 1:
                improvement = self._harness.improvements.active(
                    job,
                    action,
                    self._harness.coordinator.projection(job.job_id),
                    self._harness.clock(),
                )
            self._harness.hermes.validate_dispatch_configuration(
                FactoryDispatch(
                    job=job,
                    action=action,
                    role=role,
                    lease=lease,
                    improvement_authorization=improvement,
                )
            )
        return action

    async def dispatch_next(self, job_id: UUID):
        self.dispatch_count += 1
        action = self._harness.coordinator.next_action(job_id)
        self.actions.append(action.kind.value)
        dispatched = await self._dispatcher.dispatch_next(job_id)
        if action.kind is FactoryActionKind.DISPATCH_QUALITY_WARDEN:
            self._harness.attempt_worker_promotion()
        return dispatched


class RecordingLiveEffectLedger(InMemoryFactoryLiveEffectLedger):
    def __init__(self, events: list[str], state_path: Path) -> None:
        super().__init__()
        self._events = events
        self._state_path = state_path
        self._reload()

    def _reload(self) -> None:
        if not self._state_path.is_file():
            return
        state = _read_state(self._state_path)
        (
            self._records,
            self._effect_ids_by_key,
            self._effect_ids_by_invocation,
        ) = state  # type: ignore[misc]

    def _persist(self) -> None:
        _persist_state(
            self._state_path,
            (
                self._records,
                self._effect_ids_by_key,
                self._effect_ids_by_invocation,
            ),
        )

    def claim(self, request: FactoryLiveEffectRequestV1):
        self._events.append(f"claim:{request.kind.value}")
        self._reload()
        claim = super().claim(request)
        self._persist()
        return claim

    def complete(
        self,
        request: FactoryLiveEffectRequestV1,
        outcome: FactoryLiveEffectOutcomeV1,
    ):
        self._reload()
        receipt = super().complete(request, outcome)
        self._persist()
        return receipt


class LiveEffectPlan:
    def __init__(self, harness: "SixSkillFactoryHarness") -> None:
        self._harness = harness
        self._policy = SkillSequencePolicy()

    def effects_for(
        self,
        *,
        job: AgentFactoryJobV3,
        mode: str,
        projection: FactoryProjection,
        workflow_artifacts: tuple[object, ...],
    ) -> tuple[FactoryLiveEffectRequestV1, ...]:
        del mode, workflow_artifacts
        action = self._harness.coordinator.next_action(job.job_id)
        if action.kind is FactoryActionKind.DISPATCH_TOOL_INTEGRATOR:
            steps = self._policy.steps_for(
                role=FactoryRole.TOOL_INTEGRATOR,
                attempt=action.attempt,
            )
            return tuple(
                self._request(job, action, step, index)
                for index, step in enumerate(steps, start=1)
            )
        if action.kind is FactoryActionKind.DISPATCH_REAL_CASE_TESTER:
            return tuple(
                self._request(
                    job,
                    action,
                    FactorySkillStep.EXECUTE_TEAM,
                    run_number,
                )
                for run_number in range(
                    1, job.execution_policy.required_live_runs + 1
                )
            )
        if (
            action.kind is FactoryActionKind.COMPLETE
            and (
                self._harness._last_result is not None
                or self._harness.restart_after_evidence
            )
        ):
            replay_action = SimpleNamespace(
                kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
                attempt=projection.attempt,
            )
            return tuple(
                self._request(
                    job,
                    replay_action,
                    FactorySkillStep.EXECUTE_TEAM,
                    run_number,
                )
                for run_number in range(
                    1, job.execution_policy.required_live_runs + 1
                )
            )
        return ()

    def _request(
        self,
        job: AgentFactoryJobV3,
        action,
        step: FactorySkillStep,
        sequence: int,
    ) -> FactoryLiveEffectRequestV1:
        role = (
            FactoryRole.REAL_CASE_TESTER
            if step is FactorySkillStep.EXECUTE_TEAM
            else FactoryRole.TOOL_INTEGRATOR
        )
        dispatch = FactoryDispatch(
            job=job,
            action=action,
            role=role,
            lease=self._harness.leases.active(
                job,
                role,
                action.attempt,
                NOW,
            ),
            improvement_authorization=(
                self._harness.improvements.active(
                    job,
                    action,
                    projection=self._harness.coordinator.projection(job.job_id),
                    now=NOW,
                )
                if action.attempt > 1 and role is FactoryRole.TOOL_INTEGRATOR
                else None
            ),
        )
        if step is FactorySkillStep.EXECUTE_TEAM:
            invocation = self._harness.components.team_execution.invocation_for_run(
                dispatch,
                sequence,
            )
            identity = invocation.idempotency_key
        else:
            released = self._harness.catalog.released_for(job, step)
            invocation = _factory_invocation(
                dispatch,
                step=step,
                released_skill=released,
                input_ref=job.input_ref,
            )
            identity = hashlib.sha256(
                f"{job.job_id}:{action.attempt}:{step.value}:{sequence}".encode()
            ).hexdigest()
            invocation = invocation.model_copy(
                update={
                    "invocation_id": uuid5(
                        NAMESPACE_URL,
                        f"deterministic-live-invocation:{identity}",
                    ),
                    "idempotency_key": identity,
                }
            )
        kind = (
            FactoryLiveEffectKind.PROVIDER
            if step is FactorySkillStep.EXECUTE_TEAM
            else FactoryLiveEffectKind.CODEX
        )
        return FactoryLiveEffectRequestV1(
            schema_name="captain.factory-live-effect-request.v1",
            effect_id=uuid5(
                NAMESPACE_URL,
                f"deterministic-live-effect:{identity}",
            ),
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=action.attempt,
            kind=kind,
            idempotency_key=identity,
            input_ref=invocation.input_ref,
            invocation=invocation,
        )


class RecoveringLiveExecutor:
    def __init__(
        self,
        harness: "SixSkillFactoryHarness",
        instance_id: str,
    ) -> None:
        self._harness = harness
        self.instance_id = instance_id
        self.execute_calls = 0
        self.recover_calls = 0

    async def execute(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectOutcomeV1:
        self.execute_calls += 1
        self._harness.mark_pre_promotion()
        self._harness.effect_order.append(f"start:{request.kind.value}")
        if request.kind is FactoryLiveEffectKind.CODEX:
            await self._harness.invoke_codex(request)
            self._harness.effect_counts[request.kind.value] += 1
        else:
            self._harness._reservation_owners[request.effect_id] = self.instance_id
            await self._harness.execute_claimed_team(request)
            self._harness.effect_counts[request.kind.value] += 1
        if (
            request.kind is FactoryLiveEffectKind.PROVIDER
            and self._harness.job.execution_policy.mode.value == "release"
            and not self._harness._controlled_recovery_injected
        ):
            self._harness._controlled_recovery_injected = True
            raise FactoryInfrastructureFailure("controlled deterministic recovery")
        outcome = effect_outcome(request)
        if request.kind is FactoryLiveEffectKind.CODEX:
            self._harness.stage_codex_outcome(outcome)
        return outcome

    async def recover(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectOutcomeV1:
        self.recover_calls += 1
        self._harness._infrastructure_recoveries += 1
        self._harness.effect_order.append(f"recover:{request.kind.value}")
        outcome = effect_outcome(request)
        if request.kind is FactoryLiveEffectKind.PROVIDER:
            owner = self._harness._reservation_owners[request.effect_id]
            if owner != self.instance_id:
                self._harness._reservation_recovered_after_restart = True
            await self._harness.execute_claimed_team(request)
        if request.kind is FactoryLiveEffectKind.CODEX:
            self._harness.stage_codex_outcome(outcome)
        return outcome


class RestartableLiveRunner:
    """Durable history around newly instantiated production live runners."""

    def __init__(self, harness: "SixSkillFactoryHarness") -> None:
        self._harness = harness
        self._history: list[FactoryLiveRunReport] = []
        self.instance_ids: list[str] = []
        self._runner: FactoryLiveRunner
        self.restart()

    def restart(self) -> None:
        if self.instance_ids:
            self._harness.rehydrate_state_components()
        instance_id = f"runner-{len(self.instance_ids) + 1}"
        self.instance_ids.append(instance_id)
        executor = RecoveringLiveExecutor(self._harness, instance_id)
        self._harness.live_executor = executor
        self._harness.lifecycle.replace(FactoryCoordinator(self._harness.repository))
        self._runner = FactoryLiveRunner(
            repository=self._harness.repository,
            effect_ledger=self._harness.effect_ledger,
            plan=LiveEffectPlan(self._harness),
            executor=executor,
            clock=self._harness.clock,
        )

    def history(self, job_id: UUID) -> tuple[FactoryLiveRunReport, ...]:
        return tuple(report for report in self._history if report.job_id == job_id)

    async def run(
        self,
        job: UUID | AgentFactoryJobV3,
        *,
        mode: Literal["demo", "release"],
    ) -> FactoryLiveRunReport:
        report = await self._runner.run(job, mode=mode)
        self._history.append(report)
        if (
            report.status == "infrastructure_recovery_required"
            and self._harness.restart_after_reservation
        ):
            self.restart()
        return report


@dataclass(frozen=True)
class SixSkillHarnessResult:
    coordinator_result: object
    skill_steps: tuple[str, ...]
    attempts: int
    gateway_projection: FactoryProjection
    gateway_phases: tuple[str, ...]
    workflow_artifacts: tuple[object, ...]
    usage_receipts: tuple[object, ...]
    total_cost_usd: Decimal
    feedback: FactoryFeedbackV1
    tool_gap_severities: tuple[str, ...]
    effect_counts: dict[str, int]
    effect_order: tuple[str, ...]
    infrastructure_recoveries: int
    recovered_reservations: int
    replayed_evidence: int
    used_composed_ports: bool
    live_status: str
    captain_promoted: bool
    pre_promotion_status: str
    production_dispatch_count: int
    production_dispatch_actions: tuple[str, ...]
    budget_projection: FactoryBudgetProjection
    gateway_budget_projection: FactoryBudgetProjection
    runner_instance_ids: tuple[str, ...]
    state_component_instance_ids: dict[str, tuple[int, ...]]
    reservation_recovered_after_restart: bool
    n8n_execution_ids: tuple[str, ...]
    worker_promotion_error: str
    worker_projection_before: FactoryProjection
    worker_projection_after: FactoryProjection
    team_execution_execute_calls: int
    team_execution_provider_calls: int
    team_execution_runs: tuple[TeamExecutionEvidenceV1, ...]
    team_execution_transcript_digests: tuple[str, ...]
    authorized_holdout_refs: tuple[PrivateHoldoutRef, ...]

    @property
    def worker_ready_claim_changed_projection(self) -> bool:
        return self.worker_projection_before != self.worker_projection_after


class SixSkillFactoryHarness:
    """Compose deterministic boundaries around the production coordinator."""

    def __init__(
        self,
        *,
        mode: Literal["demo", "release"] = "release",
        first_run: Literal["passed", "behavioral_failure"] = "passed",
        tool_gap: Literal["required", "optional"] | None = None,
        failure: Literal["credential_required", "infrastructure_failure"] | None = None,
        budget_usd: Decimal = Decimal("5.00"),
        change_skill_digest: bool = False,
        restart_after_reservation: bool = False,
        restart_after_evidence: bool = False,
        with_n8n: bool = False,
    ) -> None:
        self._tmp = TemporaryDirectory(prefix="captain-six-skill-")
        self.root = Path(self._tmp.name)
        base_job = workflow_job(mode=mode)
        holdout_bodies = (b"Resolve the deterministic private release case.",)
        holdouts = (
            PrivateHoldoutRef(
                schema_name="captain.private-holdout-ref.v1",
                holdout_id="holdout-111111111111",
                uri="holdout://holdout-111111111111",
                sha256=hashlib.sha256(holdout_bodies[0]).hexdigest(),
            ),
        )
        policy = base_job.execution_policy.model_copy(
            update={"max_cost_usd": budget_usd}
        )
        self.job = base_job.model_copy(
            update={
                "execution_policy": policy,
                "private_holdout_refs": holdouts,
            }
        )
        self._holdout_bodies = dict(zip(holdouts, holdout_bodies, strict=True))
        self.first_run = first_run
        self.tool_gap = tool_gap
        self.failure = failure
        self.change_skill_digest = change_skill_digest
        self.restart_after_reservation = restart_after_reservation
        self.restart_after_evidence = restart_after_evidence
        self.with_n8n = with_n8n
        self.clock = DeterministicClock()
        self.budget = PersistentFactoryBudgetLedger(self.root / "state" / "budget.pkl")
        self.repository = WorkflowGatewayRepository(
            self.job,
            self.budget,
            self.root / "state" / "repository.pkl",
        )
        self.coordinator = FactoryCoordinator(self.repository)
        self.lifecycle = GatewayLifecycleView(self.coordinator)
        self.process_calls = 0
        self.effect_counts = {"codex": 0, "n8n": 0, "provider": 0}
        self.effect_order: list[str] = []
        self._controlled_recovery_injected = False
        self._codex_outcomes: list[FactoryLiveEffectOutcomeV1] = []
        self._reservation_owners: dict[UUID, str] = {}
        self._reservation_recovered_after_restart = False
        self._infrastructure_recoveries = 0
        self._worker_promotion_error = ""
        self._worker_projection_before = self.coordinator.projection(self.job.job_id)
        self._worker_projection_after = self._worker_projection_before
        self._worker_promotion_attempted = False
        self.sealed_candidate = _sealed_team_candidate(self.root)
        if not with_n8n:
            self.sealed_candidate = _without_candidate_tools(self.sealed_candidate)
        self.team_execution_provider_calls = 0
        self._team_attempt_by_holdout: dict[PrivateHoldoutRef, int] = {}
        self.catalog = _catalog_for(self.root, *tuple(FactorySkillStep))
        self.replay_store = FilesystemFactorySkillReplayStore(
            self.root / "state" / "skill-replays"
        )
        self.evidence_store = FilesystemFactoryEvidenceStore(self.root / "evidence")
        self.hermes = DeterministicHermes(
            self,
            settings=HermesCliSettings(
                skill_root=self.root,
                evidence_root=self.root / "evidence",
            ),
            evidence_store=self.evidence_store,
            released_skill_catalog=self.catalog,
            replay_store=self.replay_store,
            clock=self.clock,
        )
        self.leases = DeterministicLeases(self.clock)
        self.improvements = DeterministicImprovements()
        self.components = self._compose_ports()
        self.used_composed_ports = (
            self.components.hermes is self.hermes
            and self.repository.budget is self.budget
        )
        self.forge = DeterministicForgeBoundary(self)
        self.candidate_boundary = DeterministicCandidateBoundary(self)
        raw_dispatcher = FactoryDispatcher(
            coordinator=self.coordinator,
            hermes=self.components.hermes,
            forge=self.forge,
            candidate_validator=self.candidate_boundary,
            leases=self.leases,
            clock=self.clock,
            improvements=self.improvements,
        )
        self.dispatcher = DeterministicDispatcher(self, raw_dispatcher)
        self.effect_ledger = RecordingLiveEffectLedger(
            self.effect_order,
            self.root / "state" / "live-effects.pkl",
        )
        self.live_executor: RecoveringLiveExecutor
        self._state_component_instance_ids = {
            "repository": [id(self.repository)],
            "effect_ledger": [id(self.effect_ledger)],
            "replay_store": [id(self.replay_store)],
            "budget": [id(self.budget)],
            "components": [id(self.components)],
        }
        self._retired_state_components: list[object] = []
        self.live_runner = RestartableLiveRunner(self)
        self._attempt_runs: dict[int, tuple[TeamExecutionEvidenceV1, ...]] = {}
        self._feedback_by_attempt: dict[int, FactoryFeedbackV1] = {}
        self._last_result: SixSkillHarnessResult | None = None
        self._replayed_evidence = 0
        self._pre_promotion_status = "running"
        if change_skill_digest:
            first_skill = self.root / "captain-factory-discover" / "SKILL.md"
            first_skill.write_text("# digest changed after release\n", encoding="utf-8")

    async def run(self) -> SixSkillHarnessResult:
        from agenten.agent_factory.factory_live_entrypoint import (
            FactorySixSkillLiveCoordinator,
        )

        if self._last_result is not None:
            replay = await self.live_runner.run(
                self.job,
                mode=self.job.execution_policy.mode.value,
            )
            self._replayed_evidence += sum(item.replayed for item in replay.effects)
        runtime = FactorySixSkillLiveCoordinator(
            coordinator=self.lifecycle,
            repository=self.repository,
            dispatcher=self.dispatcher,
            live_runner=self.live_runner,
            clock=self.clock,
        )
        result = await runtime.run(
            self.job,
            self.job.execution_policy.mode.value,
        )
        if self.restart_after_evidence and result.status == "ready_to_use":
            self.live_runner.restart()
            replay = await self.live_runner.run(self.job, mode="release")
            self._replayed_evidence += sum(item.replayed for item in replay.effects)
        projection = self.coordinator.projection(self.job.job_id)
        feedback = self._latest_feedback()
        receipts = self.repository.workflow_usage_receipts(self.job.job_id)
        team_execution_runs = tuple(
            artifact
            for artifact in self.repository.workflow_artifacts(self.job.job_id)
            if isinstance(artifact, TeamExecutionEvidenceV1)
        )
        transcript_digests: list[str] = []
        for execution in team_execution_runs:
            output_ref = execution.execution_outcome.output_ref
            if output_ref is None:
                continue
            observation = json.loads(
                (await self.evidence_store.read(output_ref)).decode("utf-8")
            )
            transcript_digests.append(observation["transcript_sha256"])
        wrapped = SixSkillHarnessResult(
            coordinator_result=result,
            skill_steps=tuple(step.value for step in result.skill_steps),
            attempts=result.attempt,
            gateway_projection=projection,
            gateway_phases=tuple(
                block.phase.value for block in self.coordinator.blocks(self.job.job_id)
            ),
            workflow_artifacts=self.repository.workflow_artifacts(self.job.job_id),
            usage_receipts=receipts,
            total_cost_usd=sum(
                (receipt.cost_usd for receipt in receipts), Decimal("0")
            ),
            feedback=feedback,
            tool_gap_severities=tuple(gap.severity for gap in feedback.tool_gaps),
            effect_counts=dict(self.effect_counts),
            effect_order=tuple(self.effect_order),
            infrastructure_recoveries=self._infrastructure_recoveries,
            recovered_reservations=self._infrastructure_recoveries,
            replayed_evidence=self._replayed_evidence,
            used_composed_ports=self.used_composed_ports,
            live_status=(
                result.runner_reports[-1].status
                if result.runner_reports
                else result.status
            ),
            captain_promoted=result.promotion_block is not None,
            pre_promotion_status=self._pre_promotion_status,
            production_dispatch_count=self.dispatcher.dispatch_count,
            production_dispatch_actions=tuple(self.dispatcher.actions),
            budget_projection=self.budget.projection(self.job.job_id),
            gateway_budget_projection=self.repository.workflow_budget_projection(
                self.job.job_id
            ),
            runner_instance_ids=tuple(self.live_runner.instance_ids),
            state_component_instance_ids={
                name: tuple(instance_ids)
                for name, instance_ids in self._state_component_instance_ids.items()
            },
            reservation_recovered_after_restart=(
                self._reservation_recovered_after_restart
            ),
            n8n_execution_ids=tuple(
                item.execution.execution_id
                for item in (
                    self.n8n_delegate.all_evidence
                    if self.n8n_delegate is not None
                    else ()
                )
            ),
            worker_promotion_error=self._worker_promotion_error,
            worker_projection_before=self._worker_projection_before,
            worker_projection_after=self._worker_projection_after,
            team_execution_execute_calls=(
                self.components.team_execution.execute_calls
            ),
            team_execution_provider_calls=self.team_execution_provider_calls,
            team_execution_runs=team_execution_runs,
            team_execution_transcript_digests=tuple(transcript_digests),
            authorized_holdout_refs=self.job.private_holdout_refs,
        )
        self._last_result = wrapped
        return wrapped

    def artifact_for(self, invocation: FactorySkillInvocationV1):
        step = invocation.step
        attempt = invocation.attempt
        if step is FactorySkillStep.DISCOVER:
            artifact = CodebaseInventoryV1.model_validate(
                self._bound_payload(inventory_payload(), invocation)
            )
        elif step is FactorySkillStep.BRIEF_CODEX:
            payload = brief_payload()
            assignment = payload["build_assignment"]
            assert isinstance(assignment, dict)
            assignment.update(
                {
                    "correlation_id": str(invocation.correlation_id),
                    "subject_version": invocation.subject_version,
                    "attempt": attempt,
                    "idempotency_key": invocation.idempotency_key,
                    "released_skill": {
                        "skill_id": invocation.released_skill.skill_id,
                        "version": invocation.released_skill.version,
                        "content_ref": invocation.released_skill.content_ref.model_dump(
                            mode="json"
                        ),
                        "content_sha256": invocation.released_skill.content_sha256,
                    },
                    "workspace_ref": invocation.lease.workspace_ref,
                    "public_assertion_ids": list(
                        invocation.acceptance_assertion_ids
                    ),
                }
            )
            if not self._codex_outcomes:
                raise AssertionError("Codex brief was materialized before its claimed effect")
            retry_refs: list[dict[str, object]] = []
            if invocation.attempt > 1:
                authorization = _improvement_authorization()
                retry_refs = [
                    authorization.authorization_ref.model_dump(mode="json"),
                    authorization.failed_evaluation.artifact_ref.model_dump(
                        mode="json"
                    ),
                    authorization.prior_candidate_ref.model_dump(mode="json"),
                ]
            payload["context_refs"] = [
                *list(payload["context_refs"]),
                *retry_refs,
                self._codex_outcomes[-1].evidence_ref.model_dump(mode="json"),
            ]
            artifact = CodexBuildBriefV1.model_validate(
                self._bound_payload(payload, invocation)
            )
        elif step is FactorySkillStep.IMPROVE_TEAM:
            from agenten.agent_factory.skill_workflow_contracts import CandidateRevisionV1

            artifact = CandidateRevisionV1.model_validate(
                self._bound_payload(revision_payload(), invocation)
            )
        elif step is FactorySkillStep.EXECUTE_TEAM:
            artifact = self._execution_for(invocation)
        elif step is FactorySkillStep.EVALUATE_TEAM:
            artifact = self._evaluation_for(invocation)
        elif step is FactorySkillStep.REPORT_CAPTAIN:
            artifact = self._feedback_for(invocation)
        else:
            raise AssertionError(f"unhandled skill step {step.value}")
        return artifact

    def _execution_for(
        self,
        invocation: FactorySkillInvocationV1,
    ) -> TeamExecutionEvidenceV1:
        raise AssertionError(
            "execute_team must consume claimed provider results through the dispatcher"
        )

    def _evaluation_for(
        self,
        invocation: FactorySkillInvocationV1,
    ) -> TeamEvaluationV1:
        runs = self._attempt_runs[invocation.attempt]
        evaluation = workflow_evaluation(
            runs,
            budget=self.budget.projection(self.job.job_id),
        )
        if self.failure is not None:
            evaluation = evaluation.model_copy(update={"failure_class": self.failure})
        return TeamEvaluationV1.model_validate(
            self._bound_payload(
                evaluation.model_dump(mode="json", by_alias=True),
                invocation,
            )
        )

    def _feedback_for(
        self,
        invocation: FactorySkillInvocationV1,
    ) -> FactoryFeedbackV1:
        evaluation = next(
            artifact
            for artifact in reversed(
                self.repository.workflow_artifacts(self.job.job_id)
            )
            if isinstance(artifact, TeamEvaluationV1)
            and artifact.attempt == invocation.attempt
        )
        gaps: tuple[ToolGapMarker, ...] = ()
        if self.tool_gap is not None:
            gap = ToolGapMarker.model_validate(
                tool_gap_payload(severity=self.tool_gap)
            )
            gaps = (gap,)
        feedback = FactoryFeedbackBuilder(clock=self.clock).build(
            invocation=invocation,
            candidate_ref=self._attempt_runs[invocation.attempt][0].candidate_ref,
            evaluation=evaluation,
            tool_gaps=gaps,
            budget_projection=self.budget.projection(self.job.job_id),
        )
        self._feedback_by_attempt[invocation.attempt] = feedback
        return feedback

    def mark_pre_promotion(self) -> None:
        self._pre_promotion_status = self.coordinator.projection(
            self.job.job_id
        ).status.value

    def attempt_worker_promotion(self) -> None:
        if self._worker_promotion_attempted:
            return
        self._worker_promotion_attempted = True
        self._worker_projection_before = self.coordinator.projection(
            self.job.job_id
        )
        self._pre_promotion_status = self._worker_projection_before.status.value
        reference = self._worker_projection_before.feedback_ref
        if reference is None:
            reference = ArtifactRef(
                uri="artifact://deterministic/worker-promotion",
                sha256=hashlib.sha256(b"worker-promotion").hexdigest(),
                media_type="application/json",
            )
        try:
            worker_claim = FactoryEvidenceBlock(
                schema_name="captain.agent-factory-block.v1",
                event_id=uuid5(
                    NAMESPACE_URL,
                    f"worker-promotion:{self.job.job_id}",
                ),
                job_id=self.job.job_id,
                correlation_id=self.job.correlation_id,
                causation_id=self.job.event_id,
                occurred_at=self.clock(),
                producer="hermes",
                subject_version=self.job.subject_version,
                attempt=self._worker_projection_before.attempt,
                phase=FactoryPhase.CAPABILITY_PROMOTED,
                role=None,
                status=FactoryBlockStatus.SUCCEEDED,
                artifact_refs=(reference,),
                evidence_refs=(reference,),
                assertion_ids=self.job.acceptance_assertion_ids,
                lease_id=None,
            )
            self.coordinator.record(worker_claim)
        except (FactoryLifecycleError, ValueError) as exc:
            self._worker_promotion_error = f"producer rejected: {exc}"
        self._worker_projection_after = self.coordinator.projection(
            self.job.job_id
        )

    def stage_codex_outcome(self, outcome: FactoryLiveEffectOutcomeV1) -> None:
        if outcome not in self._codex_outcomes:
            self._codex_outcomes.append(outcome)

    @property
    def paid_cost_usd(self) -> Decimal:
        return self.budget.projection(self.job.job_id).consumed_usd

    async def execute_claimed_team(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> TeamExecutionEvidenceV1:
        dispatch = FactoryDispatch(
            job=self.job,
            action=SimpleNamespace(
                kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
                attempt=request.attempt,
            ),
            role=FactoryRole.REAL_CASE_TESTER,
            lease=request.invocation.lease,
        )
        candidate = self.components.candidate_provider.candidate_for(self.job)
        return await self.components.team_execution.execute_claimed(
            dispatch,
            candidate,
            request.invocation,
        )

    def model_client_for(
        self,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
    ) -> CountingReplayModelClient:
        assert job == self.job
        scope = invocation.execution_scope_ref
        if not isinstance(scope, PrivateHoldoutRef):
            raise AssertionError("team model invocation lacks a private holdout scope")
        self._team_attempt_by_holdout[scope] = invocation.attempt
        responses: list[str | CreateResult] = []
        if self.with_n8n:
            responses.append(
                CreateResult(
                    finish_reason="function_calls",
                    content=[
                        FunctionCall(
                            id=f"n8n-{invocation.invocation_id}",
                            name="support_triage",
                            arguments=json.dumps(
                                {"ticket": f"case-{scope.holdout_id}"}
                            ),
                        )
                    ],
                    usage=RequestUsage(prompt_tokens=1, completion_tokens=1),
                    cached=False,
                )
            )
        responses.append(f"completed:{invocation.invocation_id} TERMINATE")
        return CountingReplayModelClient(self, responses)

    def team_run_should_fail(self, attempt: int) -> bool:
        return (
            self.first_run == "behavioral_failure" and attempt == 1
        ) or self.failure is not None

    async def invoke_codex(self, request: FactoryLiveEffectRequestV1) -> None:
        command_id = uuid5(NAMESPACE_URL, f"codex-command:{request.effect_id}")
        subtask_id = f"factory-{request.invocation.step.value}-{request.attempt}"
        command = AgentRuntimeCommand(
            schema_name="captain.agent-runtime-command.v1",
            event_id=command_id,
            correlation_id=request.correlation_id,
            occurred_at=self.clock(),
            producer="captain",
            subject_id=subtask_id,
            subject_version=request.subject_version,
            payload=AgentRuntimeCommandPayload(
                operation=RuntimeOperation.CODEX_RUN,
                project_id="factory-team",
                batch_id="factory-codex-batch",
                subtask_id=subtask_id,
                workspace_ref=request.invocation.lease.workspace_ref,
                prompt_ref=request.input_ref,
                integration_intent=IntegrationIntent.NONE,
                capability_profile=CapabilityProfile.FACTORY_TOOL_INTEGRATOR,
                limits=RuntimeLimits(wall_seconds=60, max_iterations=2),
            ),
        )
        grant = CapabilityGrant(
            schema_name="captain.capability-grant.v1",
            grant_id=f"grant-codex-{str(command_id)[:12]}",
            command_id=command_id,
            batch_id="factory-codex-batch",
            batch_version=request.subject_version,
            subtask_id=subtask_id,
            workspace_ref=request.invocation.lease.workspace_ref,
            profile=CapabilityProfile.FACTORY_TOOL_INTEGRATOR,
            capabilities=tuple(
                sorted(
                    PROFILE_CAPABILITIES[
                        CapabilityProfile.FACTORY_TOOL_INTEGRATOR
                    ]
                )
            ),
            issued_at=self.clock(),
            expires_at=self.clock() + timedelta(minutes=5),
        )
        result = await self.components.codex.execute(command, grant)
        assert result.status is RuntimeStatus.SUCCEEDED

    def external_block(
        self,
        phase: FactoryPhase,
        *,
        attempt: int,
        producer: str,
        role: FactoryRole = FactoryRole.TOOL_INTEGRATOR,
        artifact_refs: tuple[ArtifactRef, ...] | None = None,
        evidence_refs: tuple[ArtifactRef, ...] | None = None,
        assertion_ids: tuple[str, ...] = (),
    ) -> FactoryEvidenceBlock:
        reference = ArtifactRef(
            uri=f"artifact://deterministic/{attempt}/{phase.value}",
            sha256=hashlib.sha256(
                f"{attempt}:{phase.value}".encode("utf-8")
            ).hexdigest(),
            media_type="application/json",
        )
        return FactoryEvidenceBlock(
            schema_name="captain.agent-factory-block.v1",
            event_id=uuid5(
                NAMESPACE_URL,
                f"deterministic:{self.job.job_id}:{attempt}:{phase.value}",
            ),
            job_id=self.job.job_id,
            correlation_id=self.job.correlation_id,
            causation_id=self.job.event_id,
            occurred_at=self.clock(),
            producer=producer,
            subject_version=self.job.subject_version,
            attempt=attempt,
            phase=phase,
            role=role,
            status=FactoryBlockStatus.SUCCEEDED,
            artifact_refs=artifact_refs or (reference,),
            evidence_refs=evidence_refs or (reference,),
            assertion_ids=assertion_ids,
            lease_id=f"deterministic-{attempt}-{phase.value}",
        )

    def _bound_payload(
        self,
        payload: dict[str, object],
        invocation: FactorySkillInvocationV1,
    ) -> dict[str, object]:
        bound = dict(payload)
        bound.update(
            {
                "invocation": invocation.model_dump(mode="json", by_alias=True),
                "invocation_id": str(invocation.invocation_id),
                "job_id": str(invocation.job_id),
                "correlation_id": str(invocation.correlation_id),
                "subject_version": invocation.subject_version,
                "attempt": invocation.attempt,
                "occurred_at": (NOW + timedelta(minutes=1)).isoformat(),
                "acceptance_assertion_ids": list(
                    invocation.acceptance_assertion_ids
                ),
            }
        )
        return bound

    def _latest_feedback(self) -> FactoryFeedbackV1:
        if self._feedback_by_attempt:
            return self._feedback_by_attempt[max(self._feedback_by_attempt)]
        return FactoryFeedbackV1.model_validate(feedback_payload())

    def rehydrate_state_components(self) -> None:
        """Construct a new runtime exclusively from persisted authority state."""

        self._retired_state_components.extend(
            (
                self.repository,
                self.effect_ledger,
                self.replay_store,
                self.budget,
                self.components,
            )
        )
        self.budget = PersistentFactoryBudgetLedger(self.root / "state" / "budget.pkl")
        self.repository = WorkflowGatewayRepository(
            self.job,
            self.budget,
            self.root / "state" / "repository.pkl",
        )
        self.coordinator = FactoryCoordinator(self.repository)
        self.lifecycle.replace(self.coordinator)
        self.replay_store = FilesystemFactorySkillReplayStore(
            self.root / "state" / "skill-replays"
        )
        self.evidence_store = FilesystemFactoryEvidenceStore(self.root / "evidence")
        self.hermes = DeterministicHermes(
            self,
            settings=HermesCliSettings(
                skill_root=self.root,
                evidence_root=self.root / "evidence",
            ),
            evidence_store=self.evidence_store,
            released_skill_catalog=self.catalog,
            replay_store=self.replay_store,
            clock=self.clock,
        )
        self.components = self._compose_ports()
        self.forge = DeterministicForgeBoundary(self)
        self.candidate_boundary = DeterministicCandidateBoundary(self)
        self.dispatcher.replace(
            FactoryDispatcher(
                coordinator=self.coordinator,
                hermes=self.components.hermes,
                forge=self.forge,
                candidate_validator=self.candidate_boundary,
                leases=self.leases,
                clock=self.clock,
                improvements=self.improvements,
            )
        )
        self.effect_ledger = RecordingLiveEffectLedger(
            self.effect_order,
            self.root / "state" / "live-effects.pkl",
        )
        self.used_composed_ports = (
            self.components.hermes is self.hermes
            and self.repository.budget is self.budget
        )
        current = {
            "repository": self.repository,
            "effect_ledger": self.effect_ledger,
            "replay_store": self.replay_store,
            "budget": self.budget,
            "components": self.components,
        }
        for name, component in current.items():
            self._state_component_instance_ids[name].append(id(component))

    def _compose_ports(self) -> FactoryLiveRuntimeComponents:
        integration_intent = (
            IntegrationIntent.N8N if self.with_n8n else IntegrationIntent.NONE
        )
        n8n_lease = None
        self.n8n_delegate: DeterministicN8nBoundary | None = None
        self.n8n_adapter: ScopedCaptainN8nMcpAdapter | None = None
        self.n8n_authority = DeterministicN8nAuthority()
        if self.with_n8n:
            n8n_lease = issue_factory_lease(
                job=self.job,
                role=FactoryRole.TOOL_INTEGRATOR,
                attempt=1,
                workspace_ref="workspace://factory/n8n",
                now=NOW,
                integration_intent=IntegrationIntent.N8N,
            )
            self.n8n_delegate = DeterministicN8nBoundary(
                self,
                n8n_lease.workspace_ref,
            )
            self.n8n_adapter = ScopedCaptainN8nMcpAdapter(
                lease=n8n_lease,
                delegate=self.n8n_delegate,
            )
        ports = FactoryLiveRuntimePorts(
            hermes=self.hermes,
            codex=DeterministicCodexBoundary(),
            context7=None,
            candidate_provider=DeterministicCandidateProvider(
                self.sealed_candidate
            ),
            minibook=None,
            model_client_for=self.model_client_for,
            budget=self.budget,
            pricing_source=DeterministicPricingBoundary(self.job),
            replay_store=self.replay_store,
            holdout_source=DeterministicHoldoutSource(self._holdout_bodies),
            holdout_evaluator=DeterministicHoldoutEvaluator(self),
            integration_intent=integration_intent,
            n8n_delegate=self.n8n_delegate,
            n8n_lease=n8n_lease,
            n8n_authority=self.n8n_authority,
            released_skill_catalog=self.catalog,
            skill_root=self.root,
            tools={},
            provider="deterministic-provider",
            model="approved-model-id",
            max_cost_per_call=Decimal("0.25"),
            clock=self.clock,
        )
        components = tuple(
            compose_live_factory_runtime(
                job=self.job,
                evidence_store=self.evidence_store,
                ports=ports,
                holdout_id=holdout.holdout_id,
            )
            for holdout in self.job.private_holdout_refs
        )
        return replace(
            components[0],
            team_execution=ProductionTeamExecutionRouter(
                components[0].team_execution,
                self.job.execution_policy.required_live_runs,
            ),
        )


__all__ = [
    "FIRST_PASS_STEPS",
    "RETRY_STEPS",
    "SixSkillFactoryHarness",
    "SixSkillHarnessResult",
]

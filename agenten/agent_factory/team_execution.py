"""Captain-governed execution of sealed generated AutoGen teams."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from collections.abc import Mapping
from typing import Callable, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.agent_factory.candidate_evaluation import (
    FactoryAutoGenTeamManifestV1,
    FactoryCandidateEvaluator,
    FactoryCandidateEvaluationResult,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryLease, FactoryRole
from agenten.agent_factory.evidence_store import FactoryEvidenceStore
from agenten.agent_factory.execution_budget import (
    FactoryBudgetPort,
    FactoryBudgetReservationV1,
    FactoryUsageReceiptV1,
)
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.outcome_contracts import AssertionOutcome, ExecutionOutcomeV1
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamExecutionEvidenceV1,
)
from agenten.agent_runtime.contracts import (
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    CapabilityProfile,
    IntegrationIntent,
)
from agenten.targets.n8n import N8nExecutionEvidence


class FactoryHandoffEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    from_agent: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    to_agent: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    evidence_ref: ArtifactRef


class FactoryToolExecutionEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    status: Literal["succeeded", "failed"]
    evidence_ref: ArtifactRef


class FactoryN8nExecutionEvidenceV1(BaseModel):
    """Observed n8n effect authorized by its own Captain runtime grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    capability_grant: CapabilityGrant
    runtime_result: AgentRuntimeResult
    mcp_call_id: str = Field(min_length=1, max_length=128)
    workflow_ref: ArtifactRef
    execution: N8nExecutionEvidence
    evidence_ref: ArtifactRef

    @model_validator(mode="after")
    def require_scoped_execution(self) -> "FactoryN8nExecutionEvidenceV1":
        grant = self.capability_grant
        if (
            grant.profile is not CapabilityProfile.N8N_BUILDER
            or grant.capabilities != ("mcp.n8n",)
            or grant.mcp_servers != ("n8n-mcp",)
            or self.runtime_result.grant_id != grant.grant_id
            or self.runtime_result.command_id != grant.command_id
            or self.workflow_ref.sha256 != self.execution.artifact_digest
            or not self.execution.execution_id.strip()
        ):
            raise ValueError("n8n execution evidence is not scoped or digest-matched")
        return self


class FactoryTeamRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["succeeded", "failed", "unresolved"]
    runtime_result: AgentRuntimeResult
    execution_outcome: ExecutionOutcomeV1
    usage_receipts: tuple[FactoryUsageReceiptV1, ...] = Field(max_length=1)
    handoff_evidence_refs: tuple[ArtifactRef, ...] = ()
    tool_evidence_refs: tuple[ArtifactRef, ...] = ()
    workflow_evidence_refs: tuple[ArtifactRef, ...] = ()
    handoffs: tuple[FactoryHandoffEvidenceV1, ...] = ()
    tool_executions: tuple[FactoryToolExecutionEvidenceV1, ...] = ()
    n8n_executions: tuple[FactoryN8nExecutionEvidenceV1, ...] = ()
    termination_reason: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def require_receipt_for_success(self) -> "FactoryTeamRunResult":
        if self.status == "succeeded" and not self.usage_receipts:
            raise ValueError("successful live run requires a usage receipt")
        if self.handoffs and tuple(item.evidence_ref for item in self.handoffs) != self.handoff_evidence_refs:
            raise ValueError("typed handoff evidence refs do not match")
        typed_tool_refs = tuple(item.evidence_ref for item in self.tool_executions)
        if typed_tool_refs and not set(typed_tool_refs).issubset(self.tool_evidence_refs):
            raise ValueError("typed tool evidence refs do not match")
        if self.n8n_executions and not {
            item.evidence_ref for item in self.n8n_executions
        }.issubset(self.workflow_evidence_refs):
            raise ValueError("n8n execution evidence refs do not match")
        return self


class CandidatePreflightPort(Protocol):
    def validate(
        self,
        candidate: ResolvedFactoryCandidate,
        max_seconds: float,
    ) -> FactoryCandidateEvaluationResult: ...


class FactoryTeamRunner(Protocol):
    max_cost_usd: Decimal

    async def run(
        self,
        *,
        candidate: ResolvedFactoryCandidate,
        case_ref: PrivateHoldoutRef,
        lease: FactoryLease,
        reservation: FactoryBudgetReservationV1,
        allowed_models: tuple[str, ...],
        max_seconds: float,
    ) -> FactoryTeamRunResult: ...


class SealedAutoGenTeamRunner:
    """Launch a sealed AutoGen entrypoint outside Captain's Python process."""

    def __init__(
        self,
        *,
        max_cost_usd: Decimal,
        provider_environment: Mapping[str, str],
        evaluator: FactoryCandidateEvaluator | None = None,
    ) -> None:
        if (
            isinstance(max_cost_usd, bool)
            or not isinstance(max_cost_usd, Decimal)
            or not max_cost_usd.is_finite()
            or max_cost_usd <= 0
        ):
            raise ValueError("team runner maximum cost must be a positive Decimal")
        if any(
            not isinstance(name, str)
            or not name.startswith("FACTORY_PROVIDER_")
            or not isinstance(value, str)
            or not value
            for name, value in provider_environment.items()
        ):
            raise ValueError(
                "provider environment must use explicit FACTORY_PROVIDER_ names"
            )
        self.max_cost_usd = max_cost_usd
        self._provider_environment = dict(provider_environment)
        self._evaluator = evaluator or FactoryCandidateEvaluator()
        self.last_manifest: FactoryAutoGenTeamManifestV1 | None = None

    async def run(
        self,
        *,
        candidate: ResolvedFactoryCandidate,
        case_ref: PrivateHoldoutRef,
        lease: FactoryLease,
        reservation: FactoryBudgetReservationV1,
        allowed_models: tuple[str, ...],
        max_seconds: float,
    ) -> FactoryTeamRunResult:
        with self._evaluator.verified_team_workspace(candidate) as (
            workspace,
            manifest,
        ):
            self.last_manifest = manifest
            environment = _team_environment(
                provider_environment=self._provider_environment,
                case_ref=case_ref,
                lease=lease,
                reservation=reservation,
                allowed_models=allowed_models,
            )
            options: dict[str, object]
            if os.name == "nt":
                options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            else:
                options = {"start_new_session": True}
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                *manifest.entrypoint_command[1:],
                cwd=workspace,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **options,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=max_seconds
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                await _terminate_async_process_tree(process)
                raise
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"sealed AutoGen team entrypoint failed: {detail[:500]}"
                )
            lines = stdout.decode("utf-8", errors="strict").splitlines()
            if not lines:
                raise ValueError("sealed AutoGen team returned no typed result")
            try:
                return FactoryTeamRunResult.model_validate_json(lines[-1])
            except ValueError as exc:
                raise ValueError("sealed AutoGen team returned invalid typed evidence") from exc


class TeamExecutionService:
    """Reserve paid effects only after a sealed candidate passes preflight."""

    def __init__(
        self,
        *,
        job: AgentFactoryJobV3,
        preflight: CandidatePreflightPort,
        budget: FactoryBudgetPort,
        runner: FactoryTeamRunner,
        evidence_store: FactoryEvidenceStore,
        clock: Callable[[], datetime],
    ) -> None:
        self._job = job
        self._preflight = preflight
        self._budget = budget
        self._runner = runner
        self._evidence_store = evidence_store
        self._clock = clock

    async def execute(
        self,
        invocation: FactorySkillInvocationV1,
        candidate: ResolvedFactoryCandidate,
        case_ref: PrivateHoldoutRef,
    ) -> TeamExecutionEvidenceV1:
        now = self._active_time(invocation, case_ref)
        remaining = min(
            (self._job.deadline_at - now).total_seconds(),
            (invocation.lease.expires_at - now).total_seconds(),
        )
        preflight = self._preflight.validate(candidate, remaining)
        preflight_ref = await self._evidence_store.persist(
            self._job,
            preflight.model_dump_json(exclude_none=True).encode("utf-8"),
        )
        if preflight.status != "succeeded":
            return self._failed_evidence(
                invocation,
                candidate,
                preflight_ref=preflight_ref,
            )
        if not self._job.execution_policy.live_execution:
            raise ValueError("offline factory policy forbids paid team execution")
        requested = self._runner.max_cost_usd
        if (
            isinstance(requested, bool)
            or not isinstance(requested, Decimal)
            or not requested.is_finite()
            or requested <= 0
        ):
            raise ValueError("team runner maximum cost must be a positive Decimal")
        reservation = self._budget.reserve(
            self._job,
            attempt=invocation.attempt,
            requested_usd=requested,
            now=now,
        )
        try:
            run = await asyncio.wait_for(
                self._runner.run(
                    candidate=candidate,
                    case_ref=case_ref,
                    lease=invocation.lease,
                    reservation=reservation,
                    allowed_models=self._job.execution_policy.allowed_models,
                    max_seconds=remaining,
                ),
                timeout=remaining,
            )
        except asyncio.CancelledError:
            self._budget.release(
                self._job,
                reservation,
                now=self._clock(),
                reason="cancelled",
            )
            raise
        except Exception as exc:
            self._budget.release(
                self._job,
                reservation,
                now=self._clock(),
                reason="provider_failed",
            )
            failure_ref = await self._evidence_store.persist(
                self._job,
                json.dumps(
                    {
                        "schema": "hermes.factory-provider-failure.v1",
                        "status": "unresolved",
                        "reason": "provider_cost_unresolved",
                        "error_type": type(exc).__name__,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            return self._unresolved_evidence(
                invocation,
                candidate,
                preflight_ref=preflight_ref,
                failure_ref=failure_ref,
            )
        for receipt in run.usage_receipts:
            self._budget.record_usage(self._job, reservation, receipt)
        if not run.usage_receipts:
            self._budget.release(
                self._job,
                reservation,
                now=self._clock(),
                reason="provider_failed",
            )
        self._require_run_bindings(
            invocation,
            candidate,
            run,
            topology=preflight.team_execution_manifest,
        )
        return self._run_evidence(
            invocation,
            candidate,
            preflight_ref=preflight_ref,
            run=run,
        )

    def _active_time(
        self,
        invocation: FactorySkillInvocationV1,
        case_ref: PrivateHoldoutRef,
    ) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise ValueError("team execution clock must be UTC")
        if (
            invocation.step is not FactorySkillStep.EXECUTE_TEAM
            or invocation.job_id != self._job.job_id
            or invocation.correlation_id != self._job.correlation_id
            or invocation.subject_version != self._job.subject_version
            or invocation.acceptance_assertion_ids
            != self._job.acceptance_assertion_ids
            or invocation.lease.role is not FactoryRole.REAL_CASE_TESTER
            or invocation.lease.job_id != self._job.job_id
            or invocation.lease.correlation_id != self._job.correlation_id
            or invocation.lease.attempt != invocation.attempt
            or not invocation.lease.issued_at <= now < invocation.lease.expires_at
            or not self._job.occurred_at <= now < self._job.deadline_at
        ):
            raise ValueError("team execution requires the matching active JobV3 lease")
        if case_ref not in self._job.private_holdout_refs:
            raise ValueError("team execution case is not authorized by the factory job")
        return now

    def _failed_evidence(
        self,
        invocation: FactorySkillInvocationV1,
        candidate: ResolvedFactoryCandidate,
        *,
        preflight_ref: ArtifactRef,
    ) -> TeamExecutionEvidenceV1:
        command_id = uuid5(
            NAMESPACE_URL,
            f"factory-team-preflight|{invocation.invocation_id}",
        )
        result_id = uuid5(
            NAMESPACE_URL,
            f"factory-team-preflight-result|{invocation.invocation_id}",
        )
        assertions = tuple(
            AssertionOutcome(
                assertion_id=assertion_id,
                status="failed",
                integration_intent=IntegrationIntent.NONE,
                evidence_refs=(preflight_ref,),
            )
            for assertion_id in invocation.acceptance_assertion_ids
        )
        outcome = ExecutionOutcomeV1(
            schema_name="captain.execution-outcome.v1",
            capability_id=self._job.required_capability,
            capability_version=1,
            team_version=1,
            correlation_id=invocation.correlation_id,
            command_id=command_id,
            result_id=result_id,
            output_ref=preflight_ref,
            assertion_outcomes=assertions,
            evidence_refs=(preflight_ref,),
            status="failed",
        )
        return TeamExecutionEvidenceV1(
            schema_name="hermes.factory-team-execution-evidence.v1",
            invocation=invocation,
            invocation_id=invocation.invocation_id,
            job_id=invocation.job_id,
            correlation_id=invocation.correlation_id,
            subject_version=invocation.subject_version,
            attempt=invocation.attempt,
            occurred_at=self._clock(),
            producer="hermes",
            artifact_ref=preflight_ref,
            evidence_refs=(preflight_ref,),
            acceptance_assertion_ids=invocation.acceptance_assertion_ids,
            run_number=invocation.attempt,
            candidate_ref=candidate.candidate.source_archive_ref,
            execution_outcome=outcome,
            termination_reason="preflight_failed",
            status="failed",
        )

    def _require_run_bindings(
        self,
        invocation: FactorySkillInvocationV1,
        candidate: ResolvedFactoryCandidate,
        run: FactoryTeamRunResult,
        *,
        topology: FactoryAutoGenTeamManifestV1 | None,
    ) -> None:
        runtime = run.runtime_result
        outcome = run.execution_outcome
        assertion_ids = tuple(item.assertion_id for item in outcome.assertion_outcomes)
        known_evidence = {
            *runtime.artifact_refs,
            *runtime.evidence_refs,
            *outcome.evidence_refs,
        }
        if (
            runtime.correlation_id != invocation.correlation_id
            or runtime.subject_id != candidate.candidate.candidate_id
            or runtime.subject_version != invocation.subject_version
            or outcome.correlation_id != invocation.correlation_id
            or outcome.command_id != runtime.command_id
            or outcome.result_id != runtime.event_id
            or outcome.capability_id != self._job.required_capability
            or assertion_ids != invocation.acceptance_assertion_ids
            or not set(run.handoff_evidence_refs).issubset(known_evidence)
            or not set(run.tool_evidence_refs).issubset(known_evidence)
            or not set(run.workflow_evidence_refs).issubset(known_evidence)
        ):
            raise ValueError("team run evidence does not match the Captain invocation")
        if run.status == "succeeded" and (
            runtime.status.value != "succeeded"
            or outcome.status != "succeeded"
            or any(item.status != "passed" for item in outcome.assertion_outcomes)
        ):
            raise ValueError("successful team run requires passed runtime evidence")
        if topology is not None:
            agents = {agent.name: agent for agent in topology.agents}
            if run.termination_reason not in topology.termination_conditions:
                raise ValueError("team termination is not declared by the sealed manifest")
            for handoff in run.handoffs:
                source = agents.get(handoff.from_agent)
                if source is None or handoff.to_agent not in source.handoffs:
                    raise ValueError("team handoff is not allowed by the sealed manifest")
            for tool in run.tool_executions:
                agent = agents.get(tool.agent_name)
                if agent is None or tool.tool_name not in agent.tools:
                    raise ValueError("team tool call is not allowed by the sealed manifest")
        uses_n8n = any(
            assertion.integration_intent is IntegrationIntent.N8N
            for assertion in outcome.assertion_outcomes
        )
        if uses_n8n and not run.n8n_executions:
            raise ValueError("n8n execution evidence is required for n8n activity")
        candidate_tools = {tool.name for tool in candidate.candidate.n8n_tools}
        for n8n in run.n8n_executions:
            observed_at = n8n.runtime_result.occurred_at
            if (
                n8n.tool_name not in candidate_tools
                or n8n.runtime_result.correlation_id != invocation.correlation_id
                or n8n.execution.correlation_id != str(invocation.correlation_id)
                or not n8n.capability_grant.issued_at
                <= observed_at
                < n8n.capability_grant.expires_at
                or n8n.evidence_ref not in known_evidence
            ):
                raise ValueError("n8n execution evidence does not match the Captain run")

    def _unresolved_evidence(
        self,
        invocation: FactorySkillInvocationV1,
        candidate: ResolvedFactoryCandidate,
        *,
        preflight_ref: ArtifactRef,
        failure_ref: ArtifactRef,
    ) -> TeamExecutionEvidenceV1:
        command_id = uuid5(
            NAMESPACE_URL,
            f"factory-team-provider|{invocation.invocation_id}",
        )
        result_id = uuid5(
            NAMESPACE_URL,
            f"factory-team-provider-result|{invocation.invocation_id}",
        )
        evidence_refs = (preflight_ref, failure_ref)
        assertions = tuple(
            AssertionOutcome(
                assertion_id=assertion_id,
                status="failed",
                integration_intent=IntegrationIntent.NONE,
                evidence_refs=(failure_ref,),
            )
            for assertion_id in invocation.acceptance_assertion_ids
        )
        outcome = ExecutionOutcomeV1(
            schema_name="captain.execution-outcome.v1",
            capability_id=self._job.required_capability,
            capability_version=1,
            team_version=1,
            correlation_id=invocation.correlation_id,
            command_id=command_id,
            result_id=result_id,
            output_ref=failure_ref,
            assertion_outcomes=assertions,
            evidence_refs=evidence_refs,
            status="failed",
        )
        return TeamExecutionEvidenceV1(
            schema_name="hermes.factory-team-execution-evidence.v1",
            invocation=invocation,
            invocation_id=invocation.invocation_id,
            job_id=invocation.job_id,
            correlation_id=invocation.correlation_id,
            subject_version=invocation.subject_version,
            attempt=invocation.attempt,
            occurred_at=self._clock(),
            producer="hermes",
            artifact_ref=failure_ref,
            evidence_refs=evidence_refs,
            acceptance_assertion_ids=invocation.acceptance_assertion_ids,
            run_number=invocation.attempt,
            candidate_ref=candidate.candidate.source_archive_ref,
            execution_outcome=outcome,
            termination_reason="provider_cost_unresolved",
            status="unresolved",
        )

    def _run_evidence(
        self,
        invocation: FactorySkillInvocationV1,
        candidate: ResolvedFactoryCandidate,
        *,
        preflight_ref: ArtifactRef,
        run: FactoryTeamRunResult,
    ) -> TeamExecutionEvidenceV1:
        outcome = run.execution_outcome
        artifact_ref = outcome.output_ref
        if artifact_ref is None:
            if not run.runtime_result.artifact_refs:
                raise ValueError("team run is missing a public output artifact")
            artifact_ref = run.runtime_result.artifact_refs[0]
        usage_refs = tuple(receipt.evidence_ref for receipt in run.usage_receipts)
        evidence_refs = _unique_refs(
            (
                preflight_ref,
                *run.runtime_result.artifact_refs,
                *run.runtime_result.evidence_refs,
                *outcome.evidence_refs,
                *usage_refs,
                *run.handoff_evidence_refs,
                *run.tool_evidence_refs,
                *run.workflow_evidence_refs,
            )
        )
        return TeamExecutionEvidenceV1(
            schema_name="hermes.factory-team-execution-evidence.v1",
            invocation=invocation,
            invocation_id=invocation.invocation_id,
            job_id=invocation.job_id,
            correlation_id=invocation.correlation_id,
            subject_version=invocation.subject_version,
            attempt=invocation.attempt,
            occurred_at=self._clock(),
            producer="hermes",
            artifact_ref=artifact_ref,
            evidence_refs=evidence_refs,
            acceptance_assertion_ids=invocation.acceptance_assertion_ids,
            run_number=invocation.attempt,
            candidate_ref=candidate.candidate.source_archive_ref,
            execution_outcome=outcome,
            usage_receipt_refs=usage_refs,
            handoff_evidence_refs=run.handoff_evidence_refs,
            tool_evidence_refs=run.tool_evidence_refs,
            workflow_evidence_refs=run.workflow_evidence_refs,
            termination_reason=run.termination_reason,
            status=run.status,
        )


def _unique_refs(references: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
    observed: dict[tuple[str, str, str], ArtifactRef] = {}
    for reference in references:
        key = (reference.uri, reference.sha256, reference.media_type)
        observed.setdefault(key, reference)
    return tuple(observed.values())


def _team_environment(
    *,
    provider_environment: Mapping[str, str],
    case_ref: PrivateHoldoutRef,
    lease: FactoryLease,
    reservation: FactoryBudgetReservationV1,
    allowed_models: tuple[str, ...],
) -> dict[str, str]:
    allowed_host_names = ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATHEXT")
    environment = {
        name: value
        for name in allowed_host_names
        if (value := os.environ.get(name)) is not None
    }
    environment.update(provider_environment)
    environment.update(
        {
            "PYTHONUTF8": "1",
            "CAPTAIN_FACTORY_TEAM_EXECUTION": "1",
            "CAPTAIN_CORRELATION_ID": str(lease.correlation_id),
            "CAPTAIN_HOLDOUT_URI": case_ref.uri,
            "CAPTAIN_HOLDOUT_SHA256": case_ref.sha256,
            "CAPTAIN_LEASE_ID": lease.lease_id,
            "CAPTAIN_RESERVATION_ID": str(reservation.reservation_id),
            "CAPTAIN_ALLOWED_MODELS": json.dumps(allowed_models),
        }
    )
    return environment


async def _terminate_async_process_tree(
    process: asyncio.subprocess.Process,
) -> None:
    pid = process.pid
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError("candidate process identity is invalid")
    if process.returncode is not None:
        return
    if os.name == "nt":
        cleanup = await asyncio.create_subprocess_exec(
            "taskkill.exe",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await cleanup.wait()
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await asyncio.wait_for(process.wait(), timeout=5)

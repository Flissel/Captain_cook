"""Fail-closed production entrypoint for the paid six-skill Factory gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol
from urllib.parse import unquote, urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

import pymysql

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

from agenten.agent_factory.skill_workflow_contracts import (
    FACTORY_SKILL_ID_BY_STEP,
    FactorySkillStep,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.factory_live_runner import (
    FactoryLiveEffectKind,
    FactoryLiveRunReport,
)
from agenten.agent_factory.hermes_cli import skill_directory_digest
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_factory.release_gate import FactoryReleaseDecision
from agenten.agent_factory.skill_sequence import SkillSequencePolicy
from agenten.agent_factory.state_machine import (
    FactoryAction,
    FactoryActionKind,
    FactoryLifecycleStatus,
)
from agenten.agent_runtime.contracts import ArtifactRef


FACTORY_SKILL_NAMES = tuple(
    FACTORY_SKILL_ID_BY_STEP[step]
    for step in FactorySkillStep
)
_SENSITIVE_PUBLIC_VALUE = re.compile(
    r"(?i)(?:^[A-Za-z]:[\\/]|^/|^\\\\|\bbearer\s+\S+|"
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]+|"
    r"\b(?:api[_-]?key|authorization|credential|password|secret|token|"
    r"raw[_-]?prompt)\b)"
)


class FactoryLiveConfigurationError(ValueError):
    """Public fail-closed error that never includes external command output."""


class FactoryLivePreflightSettings(BaseModel):
    """Validated local inputs; secrets are retained only as ``SecretStr`` values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["demo", "release"]
    max_cost_usd: Decimal
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    repository_root: Path
    report_directory: Path
    output: Path
    database_dsn: SecretStr
    with_n8n: bool = False

    @field_validator("max_cost_usd")
    @classmethod
    def require_positive_finite_cost(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("max_cost_usd must be a positive finite decimal")
        return value

    @model_validator(mode="after")
    def require_external_report_paths(self) -> "FactoryLivePreflightSettings":
        repository = self.repository_root.resolve()
        report_directory = self.report_directory.resolve()
        output = self.output.resolve()
        if report_directory.is_relative_to(repository):
            raise ValueError("live gate reports must remain outside the repository")
        if output.parent != report_directory:
            raise ValueError("preflight output must be inside the external report directory")
        if not repository.is_dir() or not report_directory.is_dir():
            raise ValueError("repository and external report directories must exist")
        return self


class FactoryLivePreflight(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_name: Literal["captain.hermes-six-skill-factory-preflight.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    mode: Literal["demo", "release"]
    max_cost_usd: Decimal
    model: str
    with_n8n: bool
    prerequisites_confirmed: Literal[True]
    database_name: Literal["captain_test"]
    services_verified: Literal[True]
    codex_authenticated: Literal[True]
    skills_verified: Literal[True]
    runtime_adapters_verified: Literal[True]
    skill_digests: dict[str, str]

    @field_serializer("max_cost_usd")
    def serialize_cost(self, value: Decimal) -> str:
        return _decimal_string(value)

    @model_validator(mode="after")
    def require_exact_skills(self) -> "FactoryLivePreflight":
        _require_exact_skill_digests(self.skill_digests)
        return self


class FactoryLiveProviderTrace(BaseModel):
    """Redacted provider/Codex/Hermes identity with exact Captain-booked cost."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str = Field(min_length=1, max_length=200)
    codex_session_id: str = Field(min_length=1, max_length=200)
    hermes_session_id: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    status: Literal["succeeded"]
    cost_usd: Decimal
    usage_receipt_ref: ArtifactRef
    budget_receipt_ref: ArtifactRef

    @field_validator(
        "trace_id",
        "codex_session_id",
        "hermes_session_id",
        "provider",
        "model",
    )
    @classmethod
    def require_redacted_public_values(cls, value: str) -> str:
        if _SENSITIVE_PUBLIC_VALUE.search(value):
            raise ValueError("provider trace values must be redacted")
        return value

    @field_validator("cost_usd", mode="before")
    @classmethod
    def require_known_cost(cls, value: object) -> object:
        if isinstance(value, (bool, float)) or not isinstance(value, (str, Decimal)):
            raise TypeError("provider_cost_unresolved")
        amount = Decimal(value)
        if (
            not amount.is_finite()
            or amount < 0
            or amount.as_tuple().exponent < -2
        ):
            raise ValueError("provider_cost_unresolved")
        return value

    @field_serializer("cost_usd")
    def serialize_cost(self, value: Decimal) -> str:
        return _decimal_string(value, require_fraction=True)


class FactoryLiveRecoveryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["recovered"]
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FactoryLiveN8nEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    n8n_mcp_call_id: str = Field(min_length=1, max_length=200)
    n8n_execution_id: str = Field(min_length=1, max_length=200)


class FactoryLiveObservedEvidence(BaseModel):
    """Sanitized evidence emitted by the injected production adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context7_provenance_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_traces: tuple[FactoryLiveProviderTrace, ...]
    gateway_total_cost_usd: Decimal
    recovery: FactoryLiveRecoveryEvidence | None = None
    n8n_evidence: FactoryLiveN8nEvidence | None = None

    @field_validator("gateway_total_cost_usd", mode="before")
    @classmethod
    def require_known_gateway_cost(cls, value: object) -> object:
        if isinstance(value, (bool, float)) or not isinstance(value, (str, Decimal)):
            raise TypeError("provider_cost_unresolved")
        amount = Decimal(value)
        if (
            not amount.is_finite()
            or amount < 0
            or amount.as_tuple().exponent < -2
        ):
            raise ValueError("provider_cost_unresolved")
        return value


class FactoryLiveGatewayPromotion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_status: Literal["ready_to_use"]
    release_decision: FactoryReleaseDecision
    promotion_block: FactoryEvidenceBlock


class FactoryLiveGateReport(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_name: Literal["captain.hermes-six-skill-factory-live-report.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    mode: Literal["demo", "release"]
    prerequisites_confirmed: Literal[True]
    live_execution: Literal[True]
    model: str
    database_name: Literal["captain_test"]
    context7_provenance_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    provider_traces: tuple[FactoryLiveProviderTrace, ...]
    total_cost_usd: Decimal
    terminal_status: Literal["demo_ready", "ready_to_use"]
    recovery: FactoryLiveRecoveryEvidence | None = None
    gateway_promotion: FactoryLiveGatewayPromotion | None = None
    with_n8n: bool
    n8n_evidence: FactoryLiveN8nEvidence | None = None

    @field_serializer("total_cost_usd")
    def serialize_total_cost(self, value: Decimal) -> str:
        return _decimal_string(value, require_fraction=True)


class FactoryLivePreflightProbe(Protocol):
    def verify_database(self, dsn: str) -> str: ...

    def verify_services(self) -> None: ...

    def verify_codex(self) -> None: ...

    def verify_hermes(self, expected_skill_digests: Mapping[str, str]) -> None: ...

    def verify_n8n(self) -> None: ...


class FactoryLiveLifecyclePort(Protocol):
    def next_action(self, job_id: UUID) -> FactoryAction: ...

    def projection(self, job_id: UUID) -> Any: ...

    def record(self, block: FactoryEvidenceBlock) -> bool: ...

    def promotion_block(self, job_id: UUID) -> FactoryEvidenceBlock | None: ...


class FactoryLiveWorkflowRepositoryPort(Protocol):
    def workflow_artifacts(self, job_id: UUID) -> tuple[object, ...]: ...


class FactoryLiveDispatcherPort(Protocol):
    def validate_next(
        self,
        job: AgentFactoryJobV3,
        action: FactoryAction,
        expected_skill_digests: Mapping[str, str],
    ) -> FactoryAction:
        """Stage sealed inputs and validate action/lease/digests without a process."""
        ...

    async def dispatch_next(self, job_id: UUID) -> FactoryAction:
        """Materialize claimed outcomes; never restart their external effects."""
        ...


class FactoryLiveRunnerPort(Protocol):
    def history(self, job_id: UUID) -> tuple[FactoryLiveRunReport, ...]: ...

    async def run(
        self,
        job: UUID | AgentFactoryJobV3,
        *,
        mode: Literal["demo", "release"],
    ) -> FactoryLiveRunReport: ...


class FactorySixSkillLiveResult(BaseModel):
    """One coordinator outcome rebuilt from Gateway-owned state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    correlation_id: UUID
    mode: Literal["demo", "release"]
    status: Literal[
        "blocked",
        "infrastructure_recovery_required",
        "behavioral_retry_required",
        "demo_ready",
        "ready_to_use",
        "escalated",
    ]
    attempt: int = Field(ge=1, le=5, strict=True)
    skill_steps: tuple[FactorySkillStep, ...]
    runner_reports: tuple[FactoryLiveRunReport, ...]
    gateway_projection_status: str
    promotion_block: FactoryEvidenceBlock | None = None


_DISPATCH_ROLES = {
    FactoryActionKind.DISPATCH_AGENT_ARCHITECT: FactoryRole.AGENT_ARCHITECT,
    FactoryActionKind.DISPATCH_TOOL_INTEGRATOR: FactoryRole.TOOL_INTEGRATOR,
    FactoryActionKind.DISPATCH_REAL_CASE_TESTER: FactoryRole.REAL_CASE_TESTER,
    FactoryActionKind.DISPATCH_QUALITY_WARDEN: FactoryRole.QUALITY_WARDEN,
}
_EXTERNAL_ACTIONS = frozenset(
    {
        *_DISPATCH_ROLES,
        FactoryActionKind.DISPATCH_BUILD_VALIDATOR,
        FactoryActionKind.SUBMIT_FORGE_JOB,
    }
)


class FactorySixSkillLiveCoordinator:
    """Drive exact released skills and live effects without executing candidate code."""

    def __init__(
        self,
        *,
        coordinator: FactoryLiveLifecyclePort,
        repository: FactoryLiveWorkflowRepositoryPort,
        dispatcher: FactoryLiveDispatcherPort,
        live_runner: FactoryLiveRunnerPort,
        clock: Callable[[], datetime],
        max_actions: int = 64,
    ) -> None:
        if max_actions < 1:
            raise ValueError("Factory coordinator max_actions must be positive")
        self._coordinator = coordinator
        self._repository = repository
        self._dispatcher = dispatcher
        self._live_runner = live_runner
        self._clock = clock
        self._max_actions = max_actions
        self._skill_policy = SkillSequencePolicy()
        self._released_skill_digests = _released_skill_directory_digests(
            Path(__file__).resolve().parents[2]
        )

    async def run(
        self,
        job: AgentFactoryJobV3,
        mode: Literal["demo", "release"],
    ) -> FactorySixSkillLiveResult:
        if job.execution_policy.mode.value != mode:
            raise ValueError("Factory live mode does not match Captain policy")
        projection = self._coordinator.projection(job.job_id)
        if projection.job != job:
            raise ValueError("Factory live job does not match Gateway authority")
        observed_steps: list[FactorySkillStep] = []
        runner_reports = list(self._live_runner.history(job.job_id))
        for _ in range(self._max_actions):
            action = self._coordinator.next_action(job.job_id)
            if action.kind in _EXTERNAL_ACTIONS:
                validated = self._dispatcher.validate_next(
                    job,
                    action,
                    self._released_skill_digests,
                )
                if validated != action:
                    raise ValueError(
                        "Factory dispatch preflight changed the Gateway action"
                    )
                stopped = await self._run_claimed_effects(
                    job,
                    mode,
                    action,
                    runner_reports,
                    tuple(observed_steps),
                )
                if stopped is not None:
                    return stopped
                observed_steps.extend(await self._dispatch_exact(job, action))
                continue
            if action.kind is FactoryActionKind.APPEND_FORGE_REQUESTED:
                self._coordinator.record(
                    self._captain_block(
                        job,
                        phase=FactoryPhase.FORGE_REQUESTED,
                        attempt=action.attempt,
                    )
                )
                continue
            if action.kind is FactoryActionKind.APPEND_IMPROVEMENT_REQUESTED:
                projection = self._coordinator.projection(job.job_id)
                references = _known_projection_refs(projection)
                self._coordinator.record(
                    self._captain_block(
                        job,
                        phase=FactoryPhase.IMPROVEMENT_REQUESTED,
                        attempt=action.attempt,
                        artifact_refs=references,
                        evidence_refs=references,
                    )
                )
                continue
            if action.kind is FactoryActionKind.APPEND_ESCALATED:
                self._coordinator.record(
                    self._captain_block(
                        job,
                        phase=FactoryPhase.ESCALATED,
                        attempt=action.attempt,
                    )
                )
                return self._result(
                    job,
                    mode,
                    "escalated",
                    tuple(observed_steps),
                    tuple(runner_reports),
                )
            if action.kind is FactoryActionKind.WAIT_INFRASTRUCTURE:
                return self._result(
                    job,
                    mode,
                    "infrastructure_recovery_required",
                    tuple(observed_steps),
                    tuple(runner_reports),
                )
            if action.kind is FactoryActionKind.VALIDATE_FOR_PROMOTION:
                return await self._validate_and_promote(
                    job,
                    mode,
                    tuple(observed_steps),
                    runner_reports,
                )
            if action.kind is FactoryActionKind.COMPLETE:
                status = self._coordinator.projection(job.job_id).status.value
                if status not in {"ready_to_use", "escalated"}:
                    raise ValueError("Factory completed without a terminal Gateway state")
                promotion = (
                    self._coordinator.promotion_block(job.job_id)
                    if status == "ready_to_use"
                    else None
                )
                if status == "ready_to_use" and promotion is None:
                    raise ValueError(
                        "Gateway ready_to_use state lacks its promotion block"
                    )
                return self._result(
                    job,
                    mode,
                    status,
                    tuple(observed_steps),
                    tuple(runner_reports),
                    promotion=promotion,
                )
            raise ValueError(f"unsupported Factory live action: {action.kind.value}")
        raise ValueError("Factory live coordinator exceeded its bounded action count")

    async def _run_claimed_effects(
        self,
        job: AgentFactoryJobV3,
        mode: Literal["demo", "release"],
        action: FactoryAction,
        reports: list[FactoryLiveRunReport],
        skill_steps: tuple[FactorySkillStep, ...],
    ) -> FactorySixSkillLiveResult | None:
        expected_kinds = self._required_effect_kinds(job, action)
        if not expected_kinds:
            return None
        report = await self._live_runner.run(job, mode=mode)
        reports.append(report)
        if report.status == "infrastructure_recovery_required":
            if (
                mode != "release"
                or action.kind is not FactoryActionKind.DISPATCH_REAL_CASE_TESTER
            ):
                return self._result(
                    job,
                    mode,
                    "infrastructure_recovery_required",
                    skill_steps,
                    tuple(reports),
                )
            report = await self._live_runner.run(job, mode=mode)
            reports.append(report)
        if report.status == "behavioral_retry_required":
            return self._result(
                job,
                mode,
                "behavioral_retry_required",
                skill_steps,
                tuple(reports),
            )
        if report.status == "infrastructure_recovery_required":
            return self._result(
                job,
                mode,
                "infrastructure_recovery_required",
                skill_steps,
                tuple(reports),
            )
        observed_kinds = tuple(effect.kind for effect in report.effects)
        if observed_kinds != expected_kinds or any(
            effect.status != "succeeded" for effect in report.effects
        ):
            raise ValueError(
                "Factory external dispatch lacks exact claimed live-effect evidence"
            )
        return None

    def _required_effect_kinds(
        self,
        job: AgentFactoryJobV3,
        action: FactoryAction,
    ) -> tuple[FactoryLiveEffectKind, ...]:
        if action.kind is FactoryActionKind.DISPATCH_TOOL_INTEGRATOR:
            steps = self._skill_policy.steps_for(
                role=FactoryRole.TOOL_INTEGRATOR,
                attempt=action.attempt,
            )
            return tuple(FactoryLiveEffectKind.CODEX for _ in steps)
        if action.kind is FactoryActionKind.DISPATCH_REAL_CASE_TESTER:
            return tuple(
                FactoryLiveEffectKind.PROVIDER
                for _ in range(job.execution_policy.required_live_runs)
            )
        return ()

    async def _dispatch_exact(
        self,
        job: AgentFactoryJobV3,
        action: FactoryAction,
    ) -> tuple[FactorySkillStep, ...]:
        before = self._repository.workflow_artifacts(job.job_id)
        dispatched = await self._dispatcher.dispatch_next(job.job_id)
        if dispatched != action:
            raise ValueError("Factory dispatcher executed a different Gateway action")
        after = self._repository.workflow_artifacts(job.job_id)
        if len(after) < len(before) or after[: len(before)] != before:
            raise ValueError("Factory workflow artifacts are not append-only")
        new_steps = tuple(
            artifact.invocation.step
            for artifact in after[len(before) :]
            if hasattr(artifact, "invocation")
        )
        role = _DISPATCH_ROLES.get(action.kind)
        expected = (
            self._skill_policy.steps_for(role=role, attempt=action.attempt)
            if role is not None
            else ()
        )
        if action.kind is FactoryActionKind.DISPATCH_REAL_CASE_TESTER:
            self._require_execution_batch(
                job,
                action,
                after[len(before) :],
            )
            return (FactorySkillStep.EXECUTE_TEAM,)
        if new_steps != expected:
            raise ValueError("Factory dispatch did not emit the exact released skill sequence")
        return new_steps

    @staticmethod
    def _require_execution_batch(
        job: AgentFactoryJobV3,
        action: FactoryAction,
        artifacts: tuple[object, ...],
    ) -> None:
        executions = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, TeamExecutionEvidenceV1)
        )
        required = job.execution_policy.required_live_runs
        if len(artifacts) != required or len(executions) != required:
            raise ValueError(
                "Factory execute_team dispatch must emit the exact live-run batch"
            )
        if any(
            execution.job_id != job.job_id
            or execution.correlation_id != job.correlation_id
            or execution.subject_version != job.subject_version
            or execution.attempt != action.attempt
            or execution.invocation.step is not FactorySkillStep.EXECUTE_TEAM
            for execution in executions
        ):
            raise ValueError("Factory live-run batch does not match Captain authority")
        if tuple(sorted(execution.run_number for execution in executions)) != tuple(
            range(1, required + 1)
        ):
            raise ValueError("Factory live-run batch numbers are not exact")
        if len({execution.invocation_id for execution in executions}) != required:
            raise ValueError("Factory live-run invocation identities must be distinct")
        if len(
            {execution.invocation.idempotency_key for execution in executions}
        ) != required:
            raise ValueError("Factory live-run idempotency identities must be distinct")
        if len({execution.artifact_ref for execution in executions}) != required:
            raise ValueError("Factory live-run evidence identities must be distinct")
        if len({execution.candidate_ref for execution in executions}) != 1:
            raise ValueError("Factory live-run batch must bind one candidate")
        if any(
            execution.holdout_ref not in job.private_holdout_refs
            for execution in executions
        ):
            raise ValueError("Factory live-run batch contains an unauthorized holdout")

    async def _validate_and_promote(
        self,
        job: AgentFactoryJobV3,
        mode: Literal["demo", "release"],
        skill_steps: tuple[FactorySkillStep, ...],
        reports: list[FactoryLiveRunReport],
    ) -> FactorySixSkillLiveResult:
        first = await self._live_runner.run(job, mode=mode)
        reports.append(first)
        if mode == "demo":
            if first.status != "demo_ready":
                return self._result(
                    job,
                    mode,
                    first.status,
                    skill_steps,
                    tuple(reports),
                )
            return self._result(
                job,
                mode,
                "demo_ready",
                skill_steps,
                tuple(reports),
            )
        if not any(
            report.status == "infrastructure_recovery_required"
            for report in reports[:-1]
        ):
            raise ValueError("release mode requires one controlled recovery first")
        recovered = first
        if recovered.status != "ready" or recovered.release_decision is None:
            return self._result(
                job,
                mode,
                recovered.status,
                skill_steps,
                tuple(reports),
            )
        decision = recovered.release_decision
        if decision.status != "ready" or decision.evaluation_ref is None:
            raise ValueError("release mode lacks the exact Captain evaluation decision")
        projection = self._coordinator.projection(job.job_id)
        artifact_refs = _unique_refs(
            (decision.evaluation_ref, *_known_projection_refs(projection))
        )
        promotion = self._captain_block(
            job,
            phase=FactoryPhase.CAPABILITY_PROMOTED,
            attempt=recovered.attempt,
            artifact_refs=artifact_refs,
            evidence_refs=artifact_refs,
            assertion_ids=job.acceptance_assertion_ids,
        )
        self._coordinator.record(promotion)
        reread = self._coordinator.projection(job.job_id)
        if reread.status is not FactoryLifecycleStatus.READY_TO_USE:
            raise ValueError("Gateway did not persist the Captain ready_to_use promotion")
        return self._result(
            job,
            mode,
            "ready_to_use",
            skill_steps,
            tuple(reports),
            promotion=promotion,
        )

    def _captain_block(
        self,
        job: AgentFactoryJobV3,
        *,
        phase: FactoryPhase,
        attempt: int,
        artifact_refs: tuple[ArtifactRef, ...] = (),
        evidence_refs: tuple[ArtifactRef, ...] = (),
        assertion_ids: tuple[str, ...] = (),
    ) -> FactoryEvidenceBlock:
        event_id = uuid5(
            NAMESPACE_URL,
            "|".join(
                (
                    "captain.factory-live-transition",
                    str(job.job_id),
                    str(job.subject_version),
                    str(attempt),
                    phase.value,
                )
            ),
        )
        return FactoryEvidenceBlock(
            schema_name="captain.agent-factory-block.v1",
            event_id=event_id,
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            causation_id=job.event_id,
            occurred_at=self._clock(),
            producer="captain",
            subject_version=job.subject_version,
            attempt=attempt,
            phase=phase,
            role=None,
            status=FactoryBlockStatus.SUCCEEDED,
            artifact_refs=artifact_refs,
            evidence_refs=evidence_refs,
            assertion_ids=assertion_ids,
            lease_id=None,
        )

    def _result(
        self,
        job: AgentFactoryJobV3,
        mode: Literal["demo", "release"],
        status: str,
        skill_steps: tuple[FactorySkillStep, ...],
        reports: tuple[FactoryLiveRunReport, ...],
        *,
        promotion: FactoryEvidenceBlock | None = None,
    ) -> FactorySixSkillLiveResult:
        projection = self._coordinator.projection(job.job_id)
        return FactorySixSkillLiveResult(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            mode=mode,
            status=status,
            attempt=projection.attempt,
            skill_steps=skill_steps,
            runner_reports=reports,
            gateway_projection_status=projection.status.value,
            promotion_block=promotion,
        )


def run_factory_live_preflight(
    settings: FactoryLivePreflightSettings,
    *,
    probe: FactoryLivePreflightProbe,
    adapter_factory: object | None,
) -> FactoryLivePreflight:
    """Verify real prerequisites and persist one redacted external confirmation."""

    del adapter_factory
    observed_skill_digests = _released_skill_directory_digests(
        settings.repository_root
    )
    database_name = probe.verify_database(
        settings.database_dsn.get_secret_value()
    )
    if database_name != "captain_test":
        raise FactoryLiveConfigurationError(
            "live Factory database must be the isolated captain_test database"
        )
    probe.verify_services()
    probe.verify_codex()
    probe.verify_hermes(observed_skill_digests)
    if settings.with_n8n:
        probe.verify_n8n()
    raise FactoryLiveConfigurationError(
        "production prepared dispatch adapter is unavailable"
    )


async def run_factory_live_gate_from_environment() -> Mapping[str, object]:
    """Run only from a wrapper-confirmed environment and emit one external report."""

    if os.environ.get("CAPTAIN_FACTORY_PREREQUISITES_CONFIRMED") != "1":
        raise FactoryLiveConfigurationError(
            "Factory live prerequisites were not explicitly confirmed"
        )
    settings = _settings_from_environment()
    _read_matching_preflight(settings)
    raise FactoryLiveConfigurationError(
        "production prepared dispatch adapter is unavailable"
    )


def _build_live_report(
    settings: FactoryLivePreflightSettings,
    job: AgentFactoryJobV3,
    result: FactorySixSkillLiveResult,
    observed: FactoryLiveObservedEvidence,
) -> FactoryLiveGateReport:
    expected_runs = job.execution_policy.required_live_runs
    traces = observed.provider_traces
    if len(traces) != expected_runs:
        raise FactoryLiveConfigurationError(
            "provider trace count does not match Captain execution policy"
        )
    if any(trace.model != settings.model for trace in traces):
        raise FactoryLiveConfigurationError("provider trace model is not approved")
    for values, label in (
        ((trace.trace_id for trace in traces), "trace"),
        ((trace.codex_session_id for trace in traces), "Codex session"),
        ((trace.hermes_session_id for trace in traces), "Hermes session"),
        ((trace.usage_receipt_ref for trace in traces), "usage receipt"),
        ((trace.budget_receipt_ref for trace in traces), "budget receipt"),
    ):
        identities = tuple(values)
        if len(identities) != len(set(identities)):
            raise FactoryLiveConfigurationError(
                f"Factory live {label} identities are not distinct"
            )
    total = sum((trace.cost_usd for trace in traces), Decimal("0"))
    if total != observed.gateway_total_cost_usd:
        raise FactoryLiveConfigurationError(
            "provider costs do not match the Captain Gateway budget"
        )
    if total > settings.max_cost_usd:
        raise FactoryLiveConfigurationError("Factory live cost exceeds Captain budget")
    if settings.with_n8n != (observed.n8n_evidence is not None):
        raise FactoryLiveConfigurationError("Factory n8n evidence scope does not match")

    promotion: FactoryLiveGatewayPromotion | None = None
    terminal_status: Literal["demo_ready", "ready_to_use"]
    if settings.mode == "demo":
        if result.status != "demo_ready" or result.promotion_block is not None:
            raise FactoryLiveConfigurationError("demo mode cannot promote a capability")
        if observed.recovery is not None:
            raise FactoryLiveConfigurationError("demo mode cannot claim release recovery")
        terminal_status = "demo_ready"
    else:
        if (
            result.status != "ready_to_use"
            or result.promotion_block is None
            or observed.recovery is None
        ):
            raise FactoryLiveConfigurationError(
                "release mode lacks recovery and Captain promotion"
            )
        decision = next(
            (
                report.release_decision
                for report in reversed(result.runner_reports)
                if report.release_decision is not None
                and report.release_decision.status == "ready"
            ),
            None,
        )
        if decision is None or decision.evaluation_ref is None:
            raise FactoryLiveConfigurationError(
                "release mode lacks the exact Captain release decision"
            )
        promotion = FactoryLiveGatewayPromotion(
            projection_status="ready_to_use",
            release_decision=decision,
            promotion_block=result.promotion_block,
        )
        terminal_status = "ready_to_use"
    return FactoryLiveGateReport(
        schema_name="captain.hermes-six-skill-factory-live-report.v1",
        mode=settings.mode,
        prerequisites_confirmed=True,
        live_execution=True,
        model=settings.model,
        database_name="captain_test",
        context7_provenance_digest=observed.context7_provenance_digest,
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=result.attempt,
        provider_traces=traces,
        total_cost_usd=total,
        terminal_status=terminal_status,
        recovery=observed.recovery,
        gateway_promotion=promotion,
        with_n8n=settings.with_n8n,
        n8n_evidence=observed.n8n_evidence,
    )


def _settings_from_environment() -> FactoryLivePreflightSettings:
    required = {
        name: os.environ.get(name)
        for name in (
            "CAPTAIN_FACTORY_GATE_MODE",
            "CAPTAIN_FACTORY_MAX_COST_USD",
            "CAPTAIN_FACTORY_MODEL",
            "CAPTAIN_FACTORY_REPORT_DIRECTORY",
            "CAPTAIN_FACTORY_PREFLIGHT_PATH",
            "TEST_MARIADB_DSN",
        )
    }
    if any(value is None or not value.strip() for value in required.values()):
        raise FactoryLiveConfigurationError(
            "Factory live environment is incomplete"
        )
    report_directory = Path(required["CAPTAIN_FACTORY_REPORT_DIRECTORY"] or "")
    output = Path(required["CAPTAIN_FACTORY_PREFLIGHT_PATH"] or "")
    repository_root = Path(__file__).resolve().parents[2]
    try:
        return FactoryLivePreflightSettings(
            mode=required["CAPTAIN_FACTORY_GATE_MODE"],
            max_cost_usd=required["CAPTAIN_FACTORY_MAX_COST_USD"],
            model=required["CAPTAIN_FACTORY_MODEL"],
            repository_root=repository_root,
            report_directory=report_directory,
            output=output,
            database_dsn=required["TEST_MARIADB_DSN"],
            with_n8n=os.environ.get("CAPTAIN_FACTORY_WITH_N8N") == "1",
        )
    except Exception:
        raise FactoryLiveConfigurationError(
            "Factory live environment is invalid"
        ) from None


def _read_matching_preflight(
    settings: FactoryLivePreflightSettings,
) -> FactoryLivePreflight:
    try:
        raw = json.loads(settings.output.read_text(encoding="utf-8"))
        preflight = FactoryLivePreflight.model_validate(raw)
    except Exception:
        raise FactoryLiveConfigurationError(
            "Factory live preflight confirmation is invalid"
        ) from None
    expected_digests = _released_skill_directory_digests(settings.repository_root)
    if (
        preflight.mode != settings.mode
        or preflight.max_cost_usd != settings.max_cost_usd
        or preflight.model != settings.model
        or preflight.with_n8n is not settings.with_n8n
        or preflight.skill_digests != expected_digests
    ):
        raise FactoryLiveConfigurationError(
            "Factory live preflight no longer matches the runtime"
        )
    return preflight


def _write_content_addressed_report(
    directory: Path,
    payload: Mapping[str, object],
) -> Path:
    encoded = _canonical_json_bytes(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    target = directory / f"sha256-{digest}.json"
    if target.exists() and target.read_bytes() != encoded:
        raise FactoryLiveConfigurationError(
            "Factory live report digest already binds different content"
        )
    target.write_bytes(encoded)
    return target


class SystemFactoryLivePreflightProbe:
    """Read-only local prerequisite checks with sanitized public failures."""

    def __init__(self, *, repository_root: Path) -> None:
        self._repository_root = repository_root.resolve()

    def verify_database(self, dsn: str) -> str:
        parsed = urlparse(dsn)
        database = unquote(parsed.path.lstrip("/"))
        if (
            parsed.scheme not in {"mysql", "mariadb"}
            or not parsed.hostname
            or database != "captain_test"
        ):
            raise FactoryLiveConfigurationError(
                "live Factory database must be the isolated captain_test database"
            )
        try:
            connection = pymysql.connect(
                host=parsed.hostname,
                port=parsed.port or 3306,
                user=unquote(parsed.username or ""),
                password=unquote(parsed.password or ""),
                database=database,
                connect_timeout=5,
                read_timeout=5,
                write_timeout=5,
                autocommit=True,
            )
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT DATABASE()")
                    row = cursor.fetchone()
            finally:
                connection.close()
        except Exception:
            raise FactoryLiveConfigurationError(
                "isolated Factory database is unavailable"
            ) from None
        observed = row[0] if isinstance(row, tuple) and row else None
        if observed != "captain_test":
            raise FactoryLiveConfigurationError(
                "live Factory database must be the isolated captain_test database"
            )
        return "captain_test"

    def verify_services(self) -> None:
        result = self._run(
            (
                "docker",
                "ps",
                "--filter",
                "label=com.docker.compose.service=mariadb-test",
                "--format",
                "{{.ID}}",
            )
        )
        identifiers = tuple(line for line in result.splitlines() if line.strip())
        if len(identifiers) != 1:
            raise FactoryLiveConfigurationError(
                "exactly one isolated MariaDB service is required"
            )

    def verify_codex(self) -> None:
        self._run(("codex", "login", "status"))

    def verify_hermes(self, expected_skill_digests: Mapping[str, str]) -> None:
        _require_exact_skill_digests(expected_skill_digests)
        enabled = self._run(("hermes", "skills", "list", "--enabled-only"))
        bundle = self._run(
            ("hermes", "bundles", "show", "captain-agent-factory-loop")
        )
        for name in FACTORY_SKILL_NAMES:
            if enabled.count(name) != 1 or bundle.count(name) != 1:
                raise FactoryLiveConfigurationError(
                    "Hermes does not expose the exact released Factory skills"
                )
        configured = self._run(
            ("hermes", "config", "get", "skills.external_dirs", "--json")
        )
        try:
            decoded = json.loads(configured)
            paths = (decoded,) if isinstance(decoded, str) else tuple(decoded)
            resolved = {Path(item).resolve() for item in paths if isinstance(item, str)}
        except Exception:
            raise FactoryLiveConfigurationError(
                "Hermes external skill configuration is invalid"
            ) from None
        expected_root = (
            self._repository_root / "agenten" / "agent_factory" / "skills"
        ).resolve()
        if expected_root not in resolved:
            raise FactoryLiveConfigurationError(
                "Hermes is not bound to the released Factory skill directory"
            )

    def verify_n8n(self) -> None:
        required = (
            os.environ.get("CAPTAIN_N8N_URL"),
            os.environ.get("CAPTAIN_N8N_API_KEY"),
            os.environ.get("CAPTAIN_N8N_MCP_TOKEN"),
        )
        if any(value is None or not value.strip() for value in required):
            raise FactoryLiveConfigurationError(
                "scoped Captain n8n configuration is unavailable"
            )

    @staticmethod
    def _run(arguments: tuple[str, ...]) -> str:
        try:
            completed = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
            )
        except Exception:
            raise FactoryLiveConfigurationError(
                "a required Factory live command is unavailable"
            ) from None
        if completed.returncode != 0:
            raise FactoryLiveConfigurationError(
                "a required Factory live command failed"
            )
        return completed.stdout


def _parse_arguments(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m agenten.agent_factory.factory_live_entrypoint")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--mode", choices=("demo", "release"), required=True)
    preflight.add_argument("--max-cost-usd", required=True)
    preflight.add_argument("--model", required=True)
    preflight.add_argument("--repository-root", type=Path, required=True)
    preflight.add_argument("--report-directory", type=Path, required=True)
    preflight.add_argument("--output", type=Path, required=True)
    preflight.add_argument("--with-n8n", action="store_true")
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    namespace = _parse_arguments(arguments)
    try:
        settings = FactoryLivePreflightSettings(
            mode=namespace.mode,
            max_cost_usd=namespace.max_cost_usd,
            model=namespace.model,
            repository_root=namespace.repository_root,
            report_directory=namespace.report_directory,
            output=namespace.output,
            database_dsn=os.environ.get("TEST_MARIADB_DSN", ""),
            with_n8n=namespace.with_n8n,
        )
        run_factory_live_preflight(
            settings,
            probe=SystemFactoryLivePreflightProbe(
                repository_root=settings.repository_root
            ),
            adapter_factory=None,
        )
    except Exception:
        return 1
    return 0


def _released_skill_directory_digests(repository_root: Path) -> dict[str, str]:
    root = repository_root.resolve() / "agenten" / "agent_factory" / "skills"
    try:
        return {
            name: _released_skill_digest(root / name)
            for name in FACTORY_SKILL_NAMES
        }
    except (FactoryDispatchError, OSError):
        raise FactoryLiveConfigurationError(
            "a released Factory skill is unavailable"
        ) from None


def _released_skill_digest(directory: Path) -> str:
    if not directory.is_dir() or not (directory / "SKILL.md").is_file():
        raise FactoryLiveConfigurationError("a released Factory skill is unavailable")
    return skill_directory_digest(directory)


def _known_projection_refs(projection: Any) -> tuple[ArtifactRef, ...]:
    return _unique_refs(
        tuple(
            reference
            for reference in (
                getattr(projection, "workflow_evaluation_ref", None),
                getattr(projection, "feedback_ref", None),
            )
            if isinstance(reference, ArtifactRef)
        )
    )


def _unique_refs(references: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
    unique: list[ArtifactRef] = []
    observed: set[ArtifactRef] = set()
    for reference in references:
        if reference not in observed:
            observed.add(reference)
            unique.append(reference)
    return tuple(unique)


def _require_exact_skill_digests(value: Mapping[str, str]) -> None:
    if set(value) != set(FACTORY_SKILL_NAMES) or len(value) != len(FACTORY_SKILL_NAMES):
        raise ValueError("preflight must bind exactly the six Factory skills")
    if any(
        len(digest) != 64
        or digest.lower() != digest
        or any(character not in "0123456789abcdef" for character in digest)
        for digest in value.values()
    ):
        raise ValueError("Factory skill digests must be lowercase SHA-256 values")


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_bytes(_canonical_json_bytes(payload))


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _decimal_string(value: Decimal, *, require_fraction: bool = False) -> str:
    if not value.is_finite():
        raise ValueError("USD amount must be finite")
    rendered = format(value, "f")
    if "." not in rendered:
        return f"{rendered}.0" if require_fraction else rendered
    normalized = rendered.rstrip("0").rstrip(".")
    if require_fraction and "." not in normalized:
        return f"{normalized}.0"
    return normalized


if __name__ == "__main__":
    raise SystemExit(main())

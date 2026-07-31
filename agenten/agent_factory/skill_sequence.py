"""Pure selection of Captain-authorized Hermes factory skill steps."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.business_benchmark_contracts import BusinessBenchmarkMetricId
from agenten.agent_factory.contracts import (
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.skill_workflow_contracts import (
    FactoryFeedbackRecommendation,
    FactorySkillStep,
    TeamEvaluationV1,
    factory_runtime_retry_evidence_binding,
    factory_runtime_retry_evidence_binding_sha256,
)
from agenten.agent_factory.technical_improvement_contracts import (
    CaptainTechnicalFailureEvaluationV1,
    validate_captain_technical_failure_evaluation,
)
from agenten.agent_runtime.contracts import ArtifactRef


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")


class FactoryRuntimeRetryAuthorizationV1(BaseModel):
    """One successful Captain authority for one exact Codex continuation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )

    schema_name: Literal["captain.factory-runtime-retry-authorization.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    authorization_ref: ArtifactRef
    producer: Literal["captain"]
    status: Literal["succeeded"]
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    invocation_id: UUID
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    lease_id: str = Field(min_length=1, max_length=200)
    checkpoint_ref: ArtifactRef
    terminal_receipt_ref: ArtifactRef
    workspace_ref: str = Field(min_length=1, max_length=500)
    base_revision: str
    scaffold_manifest_sha256: str
    brief_sha256: str
    resume_ordinal: int = Field(ge=1, le=2, strict=True)
    maximum_runtime_seconds: int = Field(ge=1, le=900, strict=True)
    issued_at: datetime
    expires_at: datetime

    @field_validator("producer", mode="before")
    @classmethod
    def require_captain_producer(cls, value: object) -> object:
        if value != "captain":
            raise ValueError("runtime retry authority must be Captain-produced")
        return value

    @field_validator("status", mode="before")
    @classmethod
    def require_successful_status(cls, value: object) -> object:
        if value != "succeeded":
            raise ValueError("runtime retry authority must be successful")
        return value

    @field_validator("base_revision")
    @classmethod
    def require_base_revision(cls, value: str) -> str:
        if _REVISION_PATTERN.fullmatch(value) is None:
            raise ValueError("base_revision must be a lowercase Git revision")
        return value

    @field_validator("scaffold_manifest_sha256", "brief_sha256")
    @classmethod
    def require_sha256(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("runtime retry binding must be a SHA-256 digest")
        return value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("runtime retry timestamp must be UTC")
        return value

    @model_validator(mode="after")
    def require_bounded_window(self) -> "FactoryRuntimeRetryAuthorizationV1":
        if self.expires_at <= self.issued_at:
            raise ValueError("runtime retry authority expiry must follow issuance")
        return self


class FactoryHermesReplayRetryAuthorizationV1(BaseModel):
    """One Captain authority to repeat one exact failed Hermes skill effect."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )

    schema_name: Literal["captain.factory-hermes-replay-retry-authorization.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    authorization_ref: ArtifactRef
    producer: Literal["captain"]
    status: Literal["succeeded"]
    reason: Literal["cost_ceiling_reconfigured"]
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=2, le=5, strict=True)
    invocation_id: UUID
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    lease_id: str = Field(min_length=1, max_length=200)
    step: Literal[FactorySkillStep.IMPROVE_TEAM]
    failure_kind: Literal["FactoryDispatchError"]
    failed_replay_ref: ArtifactRef
    retry_ordinal: Literal[1]
    maximum_additional_cost_usd: Decimal = Field(gt=Decimal("0"))
    prior_attempt_reserve_usd: Decimal = Field(ge=Decimal("0"))
    benchmark_reserve_usd: Decimal = Field(ge=Decimal("0"))
    internal_total_cap_usd: Decimal = Field(gt=Decimal("0"))
    user_total_cap_eur: Decimal = Field(gt=Decimal("0"))
    issued_at: datetime
    expires_at: datetime

    @field_validator("issued_at", "expires_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("Hermes retry timestamp must be UTC")
        return value

    @model_validator(mode="after")
    def require_bounded_budget_and_window(
        self,
    ) -> "FactoryHermesReplayRetryAuthorizationV1":
        if self.expires_at <= self.issued_at:
            raise ValueError("Hermes retry authority expiry must follow issuance")
        if self.internal_total_cap_usd > Decimal("0.75"):
            raise ValueError("Hermes retry internal team cap exceeds policy")
        if self.user_total_cap_eur > Decimal("1.00"):
            raise ValueError("Hermes retry user team cap exceeds policy")
        allocated = (
            self.maximum_additional_cost_usd
            + self.prior_attempt_reserve_usd
            + self.benchmark_reserve_usd
        )
        if allocated > self.internal_total_cap_usd:
            raise ValueError("Hermes retry allocations exceed internal team cap")
        return self


def build_factory_hermes_replay_retry_authorization(
    *,
    job_id: UUID,
    correlation_id: UUID,
    subject_version: int,
    attempt: int,
    invocation_id: UUID,
    idempotency_key: str,
    lease_id: str,
    failed_replay_ref: ArtifactRef,
    issued_at: datetime,
    expires_at: datetime,
    maximum_additional_cost_usd: Decimal = Decimal("0.25"),
    prior_attempt_reserve_usd: Decimal = Decimal("0.20"),
    benchmark_reserve_usd: Decimal = Decimal("0.30"),
    internal_total_cap_usd: Decimal = Decimal("0.75"),
    user_total_cap_eur: Decimal = Decimal("1.00"),
) -> FactoryHermesReplayRetryAuthorizationV1:
    """Build content-addressed, single-replay Captain recovery authority."""

    placeholder = ArtifactRef(
        uri=f"artifact://factory/hermes-replay-request/{'0' * 64}",
        sha256="0" * 64,
        media_type="application/json",
    )
    authorization = FactoryHermesReplayRetryAuthorizationV1(
        schema_name="captain.factory-hermes-replay-retry-authorization.v1",
        authorization_ref=placeholder,
        producer="captain",
        status="succeeded",
        reason="cost_ceiling_reconfigured",
        job_id=job_id,
        correlation_id=correlation_id,
        subject_version=subject_version,
        attempt=attempt,
        invocation_id=invocation_id,
        idempotency_key=idempotency_key,
        lease_id=lease_id,
        step=FactorySkillStep.IMPROVE_TEAM,
        failure_kind="FactoryDispatchError",
        failed_replay_ref=failed_replay_ref,
        retry_ordinal=1,
        maximum_additional_cost_usd=maximum_additional_cost_usd,
        prior_attempt_reserve_usd=prior_attempt_reserve_usd,
        benchmark_reserve_usd=benchmark_reserve_usd,
        internal_total_cap_usd=internal_total_cap_usd,
        user_total_cap_eur=user_total_cap_eur,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    digest = factory_hermes_replay_retry_authorization_sha256(authorization)
    return authorization.model_copy(
        update={
            "authorization_ref": ArtifactRef(
                uri=f"artifact://factory/hermes-replay-request/{digest}",
                sha256=digest,
                media_type="application/json",
            )
        }
    )


def factory_hermes_replay_retry_authorization_binding(
    authorization: FactoryHermesReplayRetryAuthorizationV1,
) -> dict[str, object]:
    return authorization.model_dump(
        mode="json",
        by_alias=True,
        exclude={"authorization_ref"},
    )


def factory_hermes_replay_retry_authorization_sha256(
    authorization: FactoryHermesReplayRetryAuthorizationV1,
) -> str:
    content = json.dumps(
        factory_hermes_replay_retry_authorization_binding(authorization),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def validate_factory_hermes_replay_retry_authorization(
    authorization: FactoryHermesReplayRetryAuthorizationV1,
    *,
    now: datetime,
) -> FactoryHermesReplayRetryAuthorizationV1:
    if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
        raise ValueError("Hermes retry validation time must be UTC")
    if now < authorization.issued_at or now >= authorization.expires_at:
        raise ValueError("Hermes retry authority is inactive or expired")
    digest = factory_hermes_replay_retry_authorization_sha256(authorization)
    if authorization.authorization_ref != ArtifactRef(
        uri=f"artifact://factory/hermes-replay-request/{digest}",
        sha256=digest,
        media_type="application/json",
    ):
        raise ValueError("Hermes retry authority content binding does not match")
    return authorization


def validate_factory_runtime_retry_authorization(
    authorization: FactoryRuntimeRetryAuthorizationV1,
    *,
    job_id: UUID,
    correlation_id: UUID,
    subject_version: int,
    attempt: int,
    invocation_id: UUID,
    idempotency_key: str,
    lease_id: str,
    checkpoint_ref: ArtifactRef,
    terminal_receipt_ref: ArtifactRef,
    workspace_ref: str,
    base_revision: str,
    scaffold_manifest_sha256: str,
    brief_sha256: str,
    current_resume_ordinal: int,
    remaining_runtime_seconds: int,
    now: datetime,
) -> FactoryRuntimeRetryAuthorizationV1:
    """Validate one authorization against the exact interrupted transition."""

    if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
        raise ValueError("runtime retry validation time must be UTC")
    identity = (
        authorization.job_id,
        authorization.correlation_id,
        authorization.subject_version,
        authorization.attempt,
        authorization.invocation_id,
        authorization.idempotency_key,
        authorization.lease_id,
    )
    expected_identity = (
        job_id,
        correlation_id,
        subject_version,
        attempt,
        invocation_id,
        idempotency_key,
        lease_id,
    )
    if identity != expected_identity:
        raise ValueError("runtime retry authority binding does not match dispatch")
    if authorization.checkpoint_ref != checkpoint_ref:
        raise ValueError("runtime retry checkpoint binding does not match")
    if authorization.terminal_receipt_ref != terminal_receipt_ref:
        raise ValueError("runtime retry receipt binding does not match")
    checkpoint_bindings = (
        authorization.workspace_ref,
        authorization.base_revision,
        authorization.scaffold_manifest_sha256,
        authorization.brief_sha256,
    )
    expected_checkpoint_bindings = (
        workspace_ref,
        base_revision,
        scaffold_manifest_sha256,
        brief_sha256,
    )
    if checkpoint_bindings != expected_checkpoint_bindings:
        raise ValueError("runtime retry checkpoint immutable binding does not match")
    if authorization.resume_ordinal != current_resume_ordinal + 1:
        raise ValueError("runtime retry ordinal is stale or already used")
    if now < authorization.issued_at:
        raise ValueError("runtime retry authority is not active")
    if now >= authorization.expires_at:
        raise ValueError("runtime retry authority is expired")
    authorization_remaining_seconds = int(
        (authorization.expires_at - now).total_seconds()
    )
    if authorization.maximum_runtime_seconds > authorization_remaining_seconds:
        raise ValueError(
            "runtime retry maximum runtime exceeds authorization window"
        )
    if (
        isinstance(remaining_runtime_seconds, bool)
        or remaining_runtime_seconds < 1
        or authorization.maximum_runtime_seconds > remaining_runtime_seconds
    ):
        raise ValueError("runtime retry maximum runtime exceeds remaining authority")
    authority_binding = factory_runtime_retry_evidence_binding(authorization)
    authority_sha256 = factory_runtime_retry_evidence_binding_sha256(
        authority_binding
    )
    expected_authority_uri = f"artifact://factory/runtime-retry/{authority_sha256}"
    if (
        authorization.authorization_ref.sha256 != authority_sha256
        or authorization.authorization_ref.uri != expected_authority_uri
        or authorization.authorization_ref.media_type != "application/json"
    ):
        raise ValueError("runtime retry authority ref content binding does not match")
    return authorization


class FactoryImprovementAuthorizationV1(BaseModel):
    """Captain evidence authorizing one retry against one failed candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.factory-improvement-authorization.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    authorization_ref: ArtifactRef
    authorized_attempt: int = Field(ge=2, le=5, strict=True)
    request_block: FactoryEvidenceBlock
    failed_evaluation: TeamEvaluationV1 | CaptainTechnicalFailureEvaluationV1
    prior_candidate_ref: ArtifactRef
    prior_green_assertion_ids: tuple[str, ...]
    prior_green_benchmark_metric_ids: tuple[BusinessBenchmarkMetricId, ...] = ()

    @field_validator("prior_green_assertion_ids")
    @classmethod
    def require_unique_prior_green_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("prior-green assertion IDs must not contain duplicates")
        return value

    @field_validator("prior_green_benchmark_metric_ids")
    @classmethod
    def require_unique_prior_green_benchmark_metric_ids(
        cls,
        value: tuple[BusinessBenchmarkMetricId, ...],
    ) -> tuple[BusinessBenchmarkMetricId, ...]:
        if len(value) != len(set(value)):
            raise ValueError(
                "prior-green benchmark metric IDs must not contain duplicates"
            )
        return value

    @model_validator(mode="after")
    def require_exact_failed_attempt_binding(self) -> "FactoryImprovementAuthorizationV1":
        request = self.request_block
        evaluation = self.failed_evaluation
        if isinstance(evaluation, CaptainTechnicalFailureEvaluationV1):
            validate_captain_technical_failure_evaluation(evaluation)
        if (
            request.phase is not FactoryPhase.IMPROVEMENT_REQUESTED
            or request.producer != "captain"
            or request.status is not FactoryBlockStatus.SUCCEEDED
        ):
            raise ValueError("improvement authorization requires a successful Captain request")
        if request.attempt + 1 != self.authorized_attempt:
            raise ValueError("improvement authorization attempt is stale")
        if (
            evaluation.job_id != request.job_id
            or evaluation.correlation_id != request.correlation_id
            or evaluation.subject_version != request.subject_version
            or evaluation.attempt != request.attempt
            or evaluation.occurred_at > request.occurred_at
        ):
            raise ValueError("failed evaluation does not match the Captain request")
        failed_ids = tuple(
            outcome.assertion_id
            for outcome in evaluation.assertion_outcomes
            if outcome.status == "failed"
        )
        failed_benchmark_metric_ids = evaluation.failed_benchmark_metric_ids
        if (
            (not failed_ids and not failed_benchmark_metric_ids)
            or evaluation.failure_class
            not in {"behavioral_failure", "test_regression"}
            or evaluation.recommendation is not FactoryFeedbackRecommendation.RETRY_BUILD
        ):
            raise ValueError("improvement authorization requires a failed evaluation")
        if evaluation.artifact_ref not in request.evidence_refs:
            raise ValueError("Captain request does not bind the failed evaluation")
        if self.prior_candidate_ref not in request.artifact_refs:
            raise ValueError("Captain request does not bind the prior candidate")
        if self.prior_green_assertion_ids != evaluation.prior_green_regression_ids:
            raise ValueError("prior-green assertions do not match the failed evaluation")
        if (
            self.prior_green_benchmark_metric_ids
            != evaluation.prior_green_benchmark_metric_ids
        ):
            raise ValueError(
                "prior-green benchmark metrics do not match the failed evaluation"
            )
        if set(failed_benchmark_metric_ids) & set(
            self.prior_green_benchmark_metric_ids
        ):
            raise ValueError("failed benchmark metrics cannot be prior-green guards")
        return self


def build_factory_improvement_authorization(
    *,
    request_block: FactoryEvidenceBlock,
    failed_evaluation: TeamEvaluationV1 | CaptainTechnicalFailureEvaluationV1,
    prior_candidate_ref: ArtifactRef,
) -> FactoryImprovementAuthorizationV1:
    """Build one content-addressed Captain authority for the next attempt."""

    placeholder = ArtifactRef(
        uri=f"artifact://factory/improvement-request/{'0' * 64}",
        sha256="0" * 64,
        media_type="application/json",
    )
    authorization = FactoryImprovementAuthorizationV1(
        schema_name="captain.factory-improvement-authorization.v1",
        authorization_ref=placeholder,
        authorized_attempt=request_block.attempt + 1,
        request_block=request_block,
        failed_evaluation=failed_evaluation,
        prior_candidate_ref=prior_candidate_ref,
        prior_green_assertion_ids=failed_evaluation.prior_green_regression_ids,
        prior_green_benchmark_metric_ids=(
            failed_evaluation.prior_green_benchmark_metric_ids
        ),
    )
    digest = factory_improvement_authorization_sha256(authorization)
    return authorization.model_copy(
        update={
            "authorization_ref": ArtifactRef(
                uri=f"artifact://factory/improvement-request/{digest}",
                sha256=digest,
                media_type="application/json",
            )
        }
    )


def factory_improvement_authorization_binding(
    authorization: FactoryImprovementAuthorizationV1,
) -> dict[str, object]:
    binding = authorization.model_dump(
        mode="json",
        by_alias=True,
        exclude={"authorization_ref"},
    )
    failed = binding.get("failed_evaluation")
    if (
        isinstance(failed, dict)
        and failed.get("schema")
        == "captain.factory-technical-failure-evaluation.v1"
        and not failed.get("technical_diagnostic_codes")
    ):
        # Preserve historical v1 authorization digests while allowing new
        # evaluations to carry bounded repair diagnostics.
        failed.pop("technical_diagnostic_codes", None)
    return binding


def factory_improvement_authorization_sha256(
    authorization: FactoryImprovementAuthorizationV1,
) -> str:
    content = json.dumps(
        factory_improvement_authorization_binding(authorization),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def validate_factory_improvement_authorization(
    authorization: FactoryImprovementAuthorizationV1,
) -> FactoryImprovementAuthorizationV1:
    if isinstance(
        authorization.failed_evaluation,
        CaptainTechnicalFailureEvaluationV1,
    ):
        validate_captain_technical_failure_evaluation(
            authorization.failed_evaluation
        )
    digest = factory_improvement_authorization_sha256(authorization)
    if authorization.authorization_ref != ArtifactRef(
        uri=f"artifact://factory/improvement-request/{digest}",
        sha256=digest,
        media_type="application/json",
    ):
        raise ValueError("improvement authorization content binding does not match")
    return authorization


class SkillSequencePolicy:
    """Map one leased factory role and attempt to its exact released steps."""

    def steps_for(
        self,
        *,
        role: FactoryRole,
        attempt: int,
        require_codex_seal: bool = True,
    ) -> tuple[FactorySkillStep, ...]:
        if isinstance(attempt, bool) or not 1 <= attempt <= 5:
            raise ValueError("factory skill attempt must be between 1 and 5")
        if role is FactoryRole.AGENT_ARCHITECT:
            return (FactorySkillStep.DISCOVER,)
        if role is FactoryRole.TOOL_INTEGRATOR:
            if attempt > 1:
                retry_steps = (
                    FactorySkillStep.IMPROVE_TEAM,
                    FactorySkillStep.BRIEF_CODEX,
                )
                return retry_steps + (
                    (FactorySkillStep.SEAL_CODEX_BUILD,)
                    if require_codex_seal
                    else ()
                )
            return (FactorySkillStep.BRIEF_CODEX,) + (
                (FactorySkillStep.SEAL_CODEX_BUILD,)
                if require_codex_seal
                else ()
            )
        if role is FactoryRole.REAL_CASE_TESTER:
            return (FactorySkillStep.EXECUTE_TEAM,)
        if role is FactoryRole.QUALITY_WARDEN:
            return (
                FactorySkillStep.EVALUATE_TEAM,
                FactorySkillStep.REPORT_CAPTAIN,
            )
        raise ValueError("unsupported factory role")

"""Fail-closed execution gate for reviewed canonical plans."""

from __future__ import annotations

from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.planning.canonical_contracts import CanonicalPlan, WorkPackageStatus
from agenten.review.contracts import (
    PlanReview,
    ReviewDecision,
    digest_plan,
    digest_review,
)
from agenten.validation.contracts import WorkBatch


EXECUTION_SCHEMA_VERSION = "captain-execution/v1"


class ExecutionNotAuthorized(RuntimeError):
    """The plan lacks trusted approval or dependency evidence."""


class ExecutionContractError(RuntimeError):
    """An execution or validation result violates its typed contract."""


class PackageExecutionStatus(str, Enum):
    REUSED = "reused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EVIDENCE_UNRESOLVED = "evidence_unresolved"


class CapabilityStatus(str, Enum):
    VALIDATED = "validated"
    REVOKED = "revoked"


class CapabilityProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_ref: str = Field(min_length=1)
    status: CapabilityStatus
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_version: int = Field(ge=1)
    target: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    contract_version: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    runtime: str = Field(min_length=1)
    runtime_version: str = Field(min_length=1)
    interface_schema: str = Field(min_length=1)
    assertion_ids: tuple[str, ...] = Field(min_length=1)
    validation_ref: str = Field(min_length=1)
    projection_version: int = Field(ge=1)

    @model_validator(mode="after")
    def assertion_ids_are_unique(self) -> "CapabilityProjection":
        if len(self.assertion_ids) != len(set(self.assertion_ids)):
            raise ValueError("assertion_ids must not contain duplicates")
        return self


class CapabilityStatusReader(Protocol):
    async def resolve(self, capability_ref: str) -> CapabilityProjection | None: ...


class ReviewDecisionReader(Protocol):
    async def resolve(self, review_id: str) -> PlanReview | None: ...


class ValidationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"


class ValidationProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str
    batch_id: str
    artifact_refs: tuple[str, ...] = Field(min_length=1)
    artifact_versions: tuple[int, ...] = Field(min_length=1)
    validation_ref: str = Field(min_length=1)
    status: ValidationStatus

    @model_validator(mode="after")
    def artifact_evidence_matches(self) -> "ValidationProjection":
        if len(self.artifact_refs) != len(self.artifact_versions):
            raise ValueError("validation artifact references and versions must align")
        if any(version < 1 for version in self.artifact_versions):
            raise ValueError("validation artifact versions must be positive")
        return self


class ValidationStatusReader(Protocol):
    async def resolve(
        self,
        plan_id: str,
        batch_id: str,
        artifact_refs: tuple[str, ...],
        artifact_versions: tuple[int, ...],
    ) -> ValidationProjection | None: ...


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = EXECUTION_SCHEMA_VERSION
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    codex_session_id: str = Field(min_length=1)
    plan_id: str
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch: WorkBatch
    worker_id: str
    handoff: str


class PackageExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    codex_session_id: str = Field(min_length=1)
    batch_id: str
    worker_id: str
    status: PackageExecutionStatus
    artifact_refs: tuple[str, ...] = ()
    artifact_versions: tuple[int, ...] = ()
    error: str | None = None

    @model_validator(mode="after")
    def status_has_consistent_evidence(self) -> "PackageExecutionResult":
        if len(self.artifact_refs) != len(self.artifact_versions):
            raise ValueError("artifact references and versions must have equal length")
        if any(version < 1 for version in self.artifact_versions):
            raise ValueError("artifact versions must be positive")
        if self.status in {
            PackageExecutionStatus.FAILED,
            PackageExecutionStatus.EVIDENCE_UNRESOLVED,
        } and not self.error:
            raise ValueError("failed execution results require an error")
        if self.status is PackageExecutionStatus.SUCCEEDED:
            if self.error:
                raise ValueError("succeeded execution results must not contain an error")
            if not self.artifact_refs:
                raise ValueError("succeeded execution results require artifact references")
        return self


class ExecutionRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = EXECUTION_SCHEMA_VERSION
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    codex_session_id: str = Field(min_length=1)
    plan_id: str
    input_sha256: str
    plan_digest: str
    review_digest: str
    status: PackageExecutionStatus
    results: tuple[PackageExecutionResult, ...]
    validations: tuple[ValidationProjection, ...] = ()


class BuildExecutor(Protocol):
    async def execute(self, request: ExecutionRequest) -> PackageExecutionResult: ...


class ExecutionProcess:
    """Execute only contracts authorized by trusted read-side projections."""

    def __init__(
        self,
        *,
        review_reader: ReviewDecisionReader,
        validation_reader: ValidationStatusReader | None = None,
        capability_reader: CapabilityStatusReader | None = None,
    ) -> None:
        self._review_reader = review_reader
        self._validation_reader = validation_reader
        self._capability_reader = capability_reader

    async def execute(
        self,
        plan: CanonicalPlan,
        review_id: str,
        executor: BuildExecutor,
        *,
        run_id: str,
        trace_id: str,
        codex_session_id: str,
    ) -> ExecutionRun:
        review = await self._resolve_review(plan, review_id)
        if any(package.status is WorkPackageStatus.PLANNED for package in plan.work_packages):
            if self._validation_reader is None:
                raise ExecutionNotAuthorized(
                    "execution requires an independent validation projection reader"
                )
        reused_projections = await self._preflight_reused_capabilities(plan)

        satisfied: set[str] = set()
        results: list[PackageExecutionResult] = []
        validations: list[ValidationProjection] = []
        for package in plan.work_packages:
            missing = sorted(set(package.depends_on) - satisfied)
            if missing:
                raise ExecutionNotAuthorized(
                    f"dependencies are not satisfied for {package.batch_id}: {missing}"
                )
            if package.status is WorkPackageStatus.REUSED:
                projection = reused_projections[package.batch_id]
                results.append(
                    PackageExecutionResult(
                        run_id=run_id,
                        trace_id=trace_id,
                        codex_session_id=codex_session_id,
                        batch_id=package.batch_id,
                        worker_id=package.worker_id,
                        status=PackageExecutionStatus.REUSED,
                        artifact_refs=(
                            f"{projection.capability_ref}#{projection.artifact_hash}",
                        ),
                        artifact_versions=(projection.artifact_version,),
                    )
                )
                satisfied.add(package.batch_id)
                continue

            request = ExecutionRequest(
                run_id=run_id,
                trace_id=trace_id,
                codex_session_id=codex_session_id,
                plan_id=plan.plan_id,
                input_sha256=plan.input_sha256,
                batch=package.batch,
                worker_id=package.worker_id,
                handoff=package.handoff,
            )
            result = await executor.execute(request)
            if (
                result.batch_id != package.batch_id
                or result.worker_id != package.worker_id
                or result.run_id != request.run_id
                or result.trace_id != request.trace_id
                or result.codex_session_id != request.codex_session_id
            ):
                raise ExecutionContractError("executor result does not match its request")
            results.append(result)
            if result.status is PackageExecutionStatus.EVIDENCE_UNRESOLVED:
                return self._run_result(
                    plan,
                    review,
                    PackageExecutionStatus.EVIDENCE_UNRESOLVED,
                    results,
                    validations,
                    run_id,
                    trace_id,
                    codex_session_id,
                )
            if result.status is PackageExecutionStatus.FAILED:
                return self._run_result(
                    plan,
                    review,
                    PackageExecutionStatus.FAILED,
                    results,
                    validations,
                    run_id,
                    trace_id,
                    codex_session_id,
                )
            if result.status is not PackageExecutionStatus.SUCCEEDED:
                raise ExecutionContractError("planned packages must succeed or fail")

            assert self._validation_reader is not None
            validation = await self._validation_reader.resolve(
                plan.plan_id,
                package.batch_id,
                result.artifact_refs,
                result.artifact_versions,
            )
            if (
                validation is None
                or validation.plan_id != plan.plan_id
                or validation.batch_id != package.batch_id
                or validation.artifact_refs != result.artifact_refs
                or validation.artifact_versions != result.artifact_versions
                or validation.status is not ValidationStatus.PASSED
            ):
                raise ExecutionContractError(
                    f"independent validation evidence missing for {package.batch_id}"
                )
            validations.append(validation)
            satisfied.add(package.batch_id)

        return self._run_result(
            plan,
            review,
            PackageExecutionStatus.SUCCEEDED,
            results,
            validations,
            run_id,
            trace_id,
            codex_session_id,
        )

    async def _resolve_review(self, plan: CanonicalPlan, review_id: str) -> PlanReview:
        review = await self._review_reader.resolve(review_id)
        if review is None or review.decision is not ReviewDecision.PASSED:
            raise ExecutionNotAuthorized("execution requires a passed review")
        if review.review_id != review_id:
            raise ExecutionNotAuthorized("review projection does not match requested review id")
        if review.plan_id != plan.plan_id or review.plan_digest != digest_plan(plan):
            raise ExecutionNotAuthorized("review does not match the exact canonical plan")
        if review.reviewer_id in plan.worker_pool:
            raise ExecutionNotAuthorized("reviewer must be independent from the worker pool")
        return review

    async def _preflight_reused_capabilities(
        self,
        plan: CanonicalPlan,
    ) -> dict[str, CapabilityProjection]:
        reused = [
            package
            for package in plan.work_packages
            if package.status is WorkPackageStatus.REUSED
        ]
        if reused and self._capability_reader is None:
            raise ExecutionNotAuthorized(
                "execution requires a validated capability projection for every reused package"
            )

        projections: dict[str, CapabilityProjection] = {}
        for package in reused:
            capability_ref = package.batch.satisfied_by
            if capability_ref is None:
                raise ExecutionNotAuthorized("reused package has no capability reference")
            assert self._capability_reader is not None
            try:
                projection = await self._capability_reader.resolve(capability_ref)
            except Exception as exc:
                raise ExecutionNotAuthorized("capability projection lookup failed") from exc
            if (
                projection is None
                or projection.capability_ref != capability_ref
                or projection.status is not CapabilityStatus.VALIDATED
            ):
                raise ExecutionNotAuthorized(
                    f"validated capability projection missing for {package.batch_id}"
                )
            batch = package.batch
            expected_assertions = tuple(
                sorted(assertion.assertion_id for assertion in batch.acceptance_criteria)
            )
            if (
                projection.target != batch.target
                or projection.contract_version != batch.contract_version
                or projection.rubric_version != batch.rubric_version
                or projection.runtime != batch.runtime
                or projection.runtime_version != batch.runtime_version
                or projection.interface_schema != batch.interface_schema
                or tuple(sorted(projection.assertion_ids)) != expected_assertions
            ):
                raise ExecutionNotAuthorized(
                    f"compatible capability projection missing for {package.batch_id}"
                )
            projections[package.batch_id] = projection
        return projections

    @staticmethod
    def _run_result(
        plan: CanonicalPlan,
        review: PlanReview,
        status: PackageExecutionStatus,
        results: list[PackageExecutionResult],
        validations: list[ValidationProjection],
        run_id: str,
        trace_id: str,
        codex_session_id: str,
    ) -> ExecutionRun:
        return ExecutionRun(
            run_id=run_id,
            trace_id=trace_id,
            codex_session_id=codex_session_id,
            plan_id=plan.plan_id,
            input_sha256=plan.input_sha256,
            plan_digest=review.plan_digest,
            review_digest=digest_review(review),
            status=status,
            results=tuple(results),
            validations=tuple(validations),
        )

import hashlib

import pytest

from agenten.execution.process import (
    CapabilityProjection,
    CapabilityStatus,
    ExecutionNotAuthorized,
    ExecutionProcess,
    ExecutionRequest,
    PackageExecutionResult,
    PackageExecutionStatus,
    ValidationProjection,
    ValidationStatus,
)
from agenten.planning.canonical_plan import CanonicalPlanCompiler
from agenten.planning.input_parser import ParsedProjectInput
from agenten.review.process import PlanReviewProcess, ReviewDecision
from agenten.validation.contracts import AcceptanceAssertion, AssertionKind, WorkBatch


RUN_ID = "run-0123456789abcdef01234567"
TRACE_ID = "trace-0123456789abcdef01234567"
CODEX_SESSION_ID = "codex-session-01"


def make_batch(batch_id: str, *, depends_on: list[str] | None = None, reused: bool = False):
    return WorkBatch(
        batch_id=batch_id,
        title=batch_id.title(),
        goal=f"Build {batch_id}",
        subtask_ids=[f"sub-{batch_id}"],
        target="n8n",
        runtime="python",
        runtime_version="3.11",
        interface_schema="captain-tool/v1",
        depends_on=depends_on or [],
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id=f"{batch_id}-done",
                kind=AssertionKind.STATUS_EQUALS,
                expected="succeeded",
            )
        ],
        satisfied_by=f"capability:{batch_id}:v1" if reused else None,
    )


def make_plan():
    project_input = ParsedProjectInput(
        source_reference="input.md",
        sha256=hashlib.sha256(b"goal").hexdigest(),
        byte_length=4,
        content="goal",
    )
    return CanonicalPlanCompiler(minimum_workers=5).compile(
        project_input,
        [
            make_batch("reused", reused=True),
            make_batch("build", depends_on=["reused"]),
        ],
    )


class RecordingExecutor:
    def __init__(self) -> None:
        self.requests: list[ExecutionRequest] = []

    async def execute(self, request: ExecutionRequest) -> PackageExecutionResult:
        self.requests.append(request)
        return PackageExecutionResult(
            run_id=request.run_id,
            trace_id=request.trace_id,
            codex_session_id=request.codex_session_id,
            batch_id=request.batch.batch_id,
            worker_id=request.worker_id,
            status=PackageExecutionStatus.SUCCEEDED,
            artifact_refs=(f"artifact:{request.batch.batch_id}:v1",),
            artifact_versions=(1,),
        )


class ValidatedCapabilityReader:
    async def resolve(self, capability_ref: str) -> CapabilityProjection | None:
        return CapabilityProjection(
            capability_ref=capability_ref,
            status=CapabilityStatus.VALIDATED,
            artifact_hash="d" * 64,
            artifact_version=1,
            target="n8n",
            contract_version="captain-work-batch/v2",
            rubric_version="captain-observation-rubric/v1",
            assertion_ids=("reused-done",),
            validation_ref="validation:reused:v1",
            projection_version=1,
            runtime="python",
            runtime_version="3.11",
            interface_schema="captain-tool/v1",
        )


class StaticReviewReader:
    def __init__(self, review=None) -> None:
        self.review = review

    async def resolve(self, review_id: str):
        if self.review is not None and self.review.review_id == review_id:
            return self.review
        return None


class PassingValidationReader:
    async def resolve(
        self,
        plan_id: str,
        batch_id: str,
        artifact_refs: tuple[str, ...],
        artifact_versions: tuple[int, ...],
    ) -> ValidationProjection | None:
        return ValidationProjection(
            plan_id=plan_id,
            batch_id=batch_id,
            artifact_refs=artifact_refs,
            artifact_versions=artifact_versions,
            validation_ref=f"validation:{batch_id}:v1",
            status=ValidationStatus.PASSED,
        )


@pytest.mark.asyncio
async def test_execution_requires_exact_passed_independent_review() -> None:
    plan = make_plan()
    executor = RecordingExecutor()
    process = ExecutionProcess(
        review_reader=StaticReviewReader(),
        validation_reader=PassingValidationReader(),
    )

    with pytest.raises(ExecutionNotAuthorized, match="passed review"):
        await process.execute(
            plan,
            "review-missing",
            executor,
            run_id=RUN_ID,
            trace_id=TRACE_ID,
            codex_session_id=CODEX_SESSION_ID,
        )

    assert executor.requests == []


@pytest.mark.asyncio
async def test_execution_skips_reused_package_and_runs_dependency_order() -> None:
    plan = make_plan()

    async def reviewer(_):
        return []

    review = await PlanReviewProcess(
        reviewer_id="quality-warden",
        reviewer_version="v1",
    ).review(plan, reviewer)
    assert review.decision is ReviewDecision.PASSED
    executor = RecordingExecutor()

    result = await ExecutionProcess(
        review_reader=StaticReviewReader(review),
        capability_reader=ValidatedCapabilityReader(),
        validation_reader=PassingValidationReader(),
    ).execute(
        plan,
        review.review_id,
        executor,
        run_id=RUN_ID,
        trace_id=TRACE_ID,
        codex_session_id=CODEX_SESSION_ID,
    )

    assert [request.batch.batch_id for request in executor.requests] == ["build"]
    assert executor.requests[0].handoff == "HANDOFF TO WORKER 2"
    assert result.status is PackageExecutionStatus.SUCCEEDED
    assert [item.batch_id for item in result.results] == ["reused", "build"]
    assert result.plan_digest == review.plan_digest
    assert result.review_digest != review.plan_digest
    assert result.validations[0].validation_ref == "validation:build:v1"
    assert executor.requests[0].run_id == result.run_id == RUN_ID
    assert executor.requests[0].trace_id == result.trace_id == TRACE_ID
    assert result.codex_session_id == CODEX_SESSION_ID
    assert result.results[1].artifact_versions == (1,)



@pytest.mark.asyncio
async def test_execution_propagates_evidence_unresolved_for_recovery() -> None:
    plan = make_plan()

    async def reviewer(_):
        return []

    review = await PlanReviewProcess(
        reviewer_id="quality-warden",
        reviewer_version="v1",
    ).review(plan, reviewer)

    class RecoveryRequiredExecutor:
        async def execute(self, request: ExecutionRequest) -> PackageExecutionResult:
            return PackageExecutionResult(
                run_id=request.run_id,
                trace_id=request.trace_id,
                codex_session_id=request.codex_session_id,
                batch_id=request.batch.batch_id,
                worker_id=request.worker_id,
                status=PackageExecutionStatus.EVIDENCE_UNRESOLVED,
                artifact_refs=("artifact:recovery/evidence-1",),
                artifact_versions=(1,),
                error="codex terminal evidence requires recovery",
            )

    class ValidationMustNotRun(PassingValidationReader):
        async def resolve(self, *args, **kwargs):
            raise AssertionError("unresolved evidence must not enter validation")

    result = await ExecutionProcess(
        review_reader=StaticReviewReader(review),
        capability_reader=ValidatedCapabilityReader(),
        validation_reader=ValidationMustNotRun(),
    ).execute(
        plan,
        review.review_id,
        RecoveryRequiredExecutor(),
        run_id=RUN_ID,
        trace_id=TRACE_ID,
        codex_session_id=CODEX_SESSION_ID,
    )

    assert result.status is PackageExecutionStatus.EVIDENCE_UNRESOLVED
    unresolved = result.results[-1]
    assert unresolved.status is PackageExecutionStatus.EVIDENCE_UNRESOLVED
    assert unresolved.artifact_refs == ("artifact:recovery/evidence-1",)
    assert unresolved.artifact_versions == (1,)
    assert unresolved.error == "codex terminal evidence requires recovery"


@pytest.mark.asyncio
async def test_reused_capabilities_fail_closed_before_any_executor_call() -> None:
    plan = make_plan()

    async def reviewer(_):
        return []

    review = await PlanReviewProcess(
        reviewer_id="quality-warden",
        reviewer_version="v1",
    ).review(plan, reviewer)
    executor = RecordingExecutor()

    with pytest.raises(ExecutionNotAuthorized, match="validated capability projection"):
        await ExecutionProcess(
            review_reader=StaticReviewReader(review),
            validation_reader=PassingValidationReader(),
        ).execute(
            plan,
            review.review_id,
            executor,
            run_id=RUN_ID,
            trace_id=TRACE_ID,
            codex_session_id=CODEX_SESSION_ID,
        )

    assert executor.requests == []


@pytest.mark.asyncio
async def test_reused_capability_must_match_the_exact_batch_contract() -> None:
    plan = make_plan()

    async def reviewer(_):
        return []

    review = await PlanReviewProcess(
        reviewer_id="quality-warden",
        reviewer_version="v1",
    ).review(plan, reviewer)
    executor = RecordingExecutor()

    class IncompatibleCapabilityReader(ValidatedCapabilityReader):
        async def resolve(self, capability_ref: str) -> CapabilityProjection | None:
            compatible = await super().resolve(capability_ref)
            assert compatible is not None
            return compatible.model_copy(update={"runtime_version": "3.12"})

    with pytest.raises(ExecutionNotAuthorized, match="compatible capability projection"):
        await ExecutionProcess(
            review_reader=StaticReviewReader(review),
            capability_reader=IncompatibleCapabilityReader(),
            validation_reader=PassingValidationReader(),
        ).execute(
            plan,
            review.review_id,
            executor,
            run_id=RUN_ID,
            trace_id=TRACE_ID,
            codex_session_id=CODEX_SESSION_ID,
        )

    assert executor.requests == []

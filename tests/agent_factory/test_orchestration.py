from __future__ import annotations

import pytest
from datetime import datetime, timezone
from uuid import UUID, uuid4

from agenten.agent_factory.contracts import (
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.orchestration import FactoryDispatchError, FactoryDispatcher
from agenten.agent_factory.forge_contracts import CreationResultV1, CreationSubmissionReceipt
from agenten.agent_factory.leases import issue_factory_lease, validate_factory_lease
from agenten.agent_factory.service import FactoryCoordinator, InMemoryFactoryRepository
from agenten.agent_factory.state_machine import FactoryActionKind
from agenten.agent_factory.skill_sequence import FactoryImprovementAuthorizationV1
from agenten.agent_factory.skill_workflow_contracts import TeamEvaluationV1
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_skill_workflow_contracts import evaluation_payload
from tests.agent_factory.test_state_machine import block, job


class Hermes:
    def __init__(self) -> None:
        self.requests = []

    async def dispatch(self, request: object):
        self.requests.append(request)
        return block(FactoryPhase.BLUEPRINT_CREATED)


class Forge:
    def __init__(self, submission=None, result=None) -> None:
        self.requests = []
        self.submission = submission
        self.result_value = result
        self.waited_for = []

    async def submit(self, request: object):
        self.requests.append(request)
        return self.submission

    async def wait_for_result(self, creation_job_id):
        self.waited_for.append(creation_job_id)
        return self.result_value


class CandidateValidator:
    def __init__(self) -> None:
        self.requests = []
        self.creation_results = []

    async def dispatch(self, request: object):
        self.requests.append(request)
        return block(FactoryPhase.BUILD_PASSED)

    async def record_creation_result(self, request, result):
        self.creation_results.append((request, result))
        return block(FactoryPhase.AGENT_CODE_CREATED)


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 7, 19, 10, tzinfo=timezone.utc)


class Leases:
    def active(self, factory_job, role, attempt, now):
        lease = issue_factory_lease(
            job=factory_job,
            role=role,
            attempt=attempt,
            workspace_ref="workspace://factory/support-triage",
            now=now,
        )
        return validate_factory_lease(lease, job=factory_job, role=role, attempt=attempt, now=now)


def dispatcher(
    coordinator,
    hermes,
    forge,
    validator=None,
    improvements=None,
) -> FactoryDispatcher:
    return FactoryDispatcher(
        coordinator=coordinator,
        hermes=hermes,
        forge=forge,
        candidate_validator=validator,
        leases=Leases(),
        clock=Clock(),
        improvements=improvements,
    )


def _creation_result(factory_job, *, creation_job_id=None) -> CreationResultV1:
    return CreationResultV1(
        creation_job_id=creation_job_id or uuid4(),
        correlation_id=factory_job.correlation_id,
        subject_version=factory_job.subject_version,
        attempt=1,
        status="succeeded",
        package_manifest_ref={
            "uri": "artifact://factory/package-manifest",
            "sha256": "a" * 64,
            "media_type": "application/json",
        },
        artifact_refs=(
            {
                "uri": "artifact://factory/generated-code",
                "sha256": "b" * 64,
                "media_type": "application/zip",
            },
        ),
        skill_usage_receipt_ref={
            "uri": "artifact://factory/skill-usage",
            "sha256": "c" * 64,
            "media_type": "application/json",
        },
    )


@pytest.mark.asyncio
async def test_dispatches_architect_only_after_captain_forge_request() -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())
    factory_job = job()
    coordinator.register(factory_job)
    coordinator.record(block(FactoryPhase.FORGE_REQUESTED))
    hermes, forge = Hermes(), Forge()

    action = await dispatcher(coordinator, hermes, forge).dispatch_next(factory_job.job_id)

    assert action.kind is FactoryActionKind.DISPATCH_AGENT_ARCHITECT
    assert hermes.requests[0].role is FactoryRole.AGENT_ARCHITECT
    assert hermes.requests[0].lease is not None
    assert coordinator.projection(factory_job.job_id).phase is FactoryPhase.BLUEPRINT_CREATED
    assert forge.requests == []


@pytest.mark.asyncio
async def test_dispatches_forge_only_after_tool_candidate_evidence() -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())
    factory_job = job()
    coordinator.register(factory_job)
    coordinator.record(block(FactoryPhase.FORGE_REQUESTED))
    coordinator.record(block(FactoryPhase.BLUEPRINT_CREATED))
    coordinator.record(block(FactoryPhase.TOOL_CANDIDATE_TESTED))
    creation_result = _creation_result(factory_job)
    hermes, forge, validator = Hermes(), Forge(creation_result), CandidateValidator()

    action = await dispatcher(coordinator, hermes, forge, validator).dispatch_next(factory_job.job_id)

    assert action.kind is FactoryActionKind.SUBMIT_FORGE_JOB
    assert len(forge.requests) == 1
    assert validator.creation_results[0][1] is creation_result
    assert validator.creation_results[0][0].action.kind is FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE
    assert validator.creation_results[0][0].role is FactoryRole.TOOL_INTEGRATOR
    assert coordinator.projection(factory_job.job_id).phase is FactoryPhase.AGENT_CODE_CREATED


@pytest.mark.asyncio
async def test_waits_for_queued_forge_result_before_recording_agent_code() -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())
    factory_job = job()
    coordinator.register(factory_job)
    for phase in (
        FactoryPhase.FORGE_REQUESTED,
        FactoryPhase.BLUEPRINT_CREATED,
        FactoryPhase.TOOL_CANDIDATE_TESTED,
    ):
        coordinator.record(block(phase))
    creation_job_id = uuid4()
    receipt = CreationSubmissionReceipt(
        creation_job_id=creation_job_id,
        status="queued",
        subject_version=factory_job.subject_version,
    )
    creation_result = _creation_result(factory_job, creation_job_id=creation_job_id)
    forge = Forge(receipt, creation_result)
    validator = CandidateValidator()

    await dispatcher(coordinator, Hermes(), forge, validator).dispatch_next(factory_job.job_id)

    assert forge.waited_for == [creation_job_id]
    assert validator.creation_results[0][1] is creation_result
    assert coordinator.projection(factory_job.job_id).phase is FactoryPhase.AGENT_CODE_CREATED


@pytest.mark.asyncio
async def test_rejects_mismatched_polled_forge_result_without_code_evidence() -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())
    factory_job = job()
    coordinator.register(factory_job)
    for phase in (
        FactoryPhase.FORGE_REQUESTED,
        FactoryPhase.BLUEPRINT_CREATED,
        FactoryPhase.TOOL_CANDIDATE_TESTED,
    ):
        coordinator.record(block(phase))
    receipt_id = uuid4()
    receipt = CreationSubmissionReceipt(
        creation_job_id=receipt_id,
        status="queued",
        subject_version=factory_job.subject_version,
    )
    forge = Forge(receipt, _creation_result(factory_job, creation_job_id=uuid4()))
    validator = CandidateValidator()

    with pytest.raises(FactoryDispatchError, match="creation job"):
        await dispatcher(coordinator, Hermes(), forge, validator).dispatch_next(factory_job.job_id)

    assert validator.creation_results == []
    assert coordinator.projection(factory_job.job_id).phase is FactoryPhase.TOOL_CANDIDATE_TESTED


@pytest.mark.asyncio
async def test_captain_transition_is_not_dispatched_externally() -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())
    factory_job = job()
    coordinator.register(factory_job)
    hermes, forge = Hermes(), Forge()

    with pytest.raises(FactoryDispatchError, match="Captain state transition"):
        await dispatcher(coordinator, hermes, forge).dispatch_next(factory_job.job_id)


@pytest.mark.asyncio
async def test_dispatches_candidate_build_validator_after_agent_code_evidence() -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())
    factory_job = job()
    coordinator.register(factory_job)
    for phase in (
        FactoryPhase.FORGE_REQUESTED,
        FactoryPhase.BLUEPRINT_CREATED,
        FactoryPhase.TOOL_CANDIDATE_TESTED,
        FactoryPhase.AGENT_CODE_CREATED,
    ):
        coordinator.record(block(phase))
    hermes, forge, validator = Hermes(), Forge(), CandidateValidator()

    action = await dispatcher(coordinator, hermes, forge, validator).dispatch_next(factory_job.job_id)

    assert action.kind is FactoryActionKind.DISPATCH_BUILD_VALIDATOR
    assert validator.requests[0].role is FactoryRole.TOOL_INTEGRATOR
    assert coordinator.projection(factory_job.job_id).phase is FactoryPhase.BUILD_PASSED
    assert hermes.requests == []


def _retry_authorization(factory_job) -> FactoryImprovementAuthorizationV1:
    evaluation_data = evaluation_payload(
        failure_class="behavioral_failure",
        recommendation="RETRY_BUILD",
        prior_green_regression_ids=["schema_valid"],
    )
    invocation = evaluation_data["invocation"]
    assert isinstance(invocation, dict)
    invocation.update(
        {
            "job_id": str(factory_job.job_id),
            "correlation_id": str(factory_job.correlation_id),
        }
    )
    lease = invocation["lease"]
    assert isinstance(lease, dict)
    lease.update(
        {
            "job_id": str(factory_job.job_id),
            "correlation_id": str(factory_job.correlation_id),
        }
    )
    outcomes = evaluation_data["assertion_outcomes"]
    assert isinstance(outcomes, list)
    failed = outcomes[1]
    assert isinstance(failed, dict)
    failed["status"] = "failed"
    evaluation_data.update(
        {
            "job_id": str(factory_job.job_id),
            "correlation_id": str(factory_job.correlation_id),
        }
    )
    evaluation = TeamEvaluationV1.model_validate(evaluation_data)
    prior_candidate = ArtifactRef(
        uri="artifact://factory/prior-candidate",
        sha256="9" * 64,
        media_type="application/zip",
    )
    request_data = block(FactoryPhase.IMPROVEMENT_REQUESTED).model_dump(
        mode="json",
        by_alias=True,
    )
    request_data["occurred_at"] = evaluation.occurred_at.isoformat()
    request_data["artifact_refs"] = [prior_candidate.model_dump(mode="json")]
    request_data["evidence_refs"] = [
        evaluation.artifact_ref.model_dump(mode="json")
    ]
    return FactoryImprovementAuthorizationV1(
        schema_name="captain.factory-improvement-authorization.v1",
        authorization_ref=ArtifactRef(
            uri="artifact://factory/improvement-request",
            sha256="8" * 64,
            media_type="application/json",
        ),
        authorized_attempt=2,
        request_block=FactoryEvidenceBlock.model_validate(request_data),
        failed_evaluation=evaluation,
        prior_candidate_ref=prior_candidate,
        prior_green_assertion_ids=("schema_valid",),
        prior_green_benchmark_metric_ids=("coverage",),
    )


@pytest.mark.asyncio
async def test_retry_dispatch_gets_typed_improvement_authorization_from_port() -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())
    factory_job = job()
    coordinator.register(factory_job)
    for phase in (
        FactoryPhase.FORGE_REQUESTED,
        FactoryPhase.BLUEPRINT_CREATED,
        FactoryPhase.TOOL_CANDIDATE_TESTED,
        FactoryPhase.AGENT_CODE_CREATED,
        FactoryPhase.BUILD_FAILED,
        FactoryPhase.IMPROVEMENT_REQUESTED,
    ):
        coordinator.record(block(phase))
    coordinator.record(
        block(FactoryPhase.BLUEPRINT_CREATED, attempt=2).model_copy(
            update={"event_id": UUID("00000000-0000-0000-0000-000000000099")}
        )
    )
    authorization = _retry_authorization(factory_job)

    class Improvements:
        def active(self, job, action, projection, now):
            assert job == factory_job
            assert action.attempt == projection.attempt == 2
            assert now == Clock().now()
            return authorization

    class RetryHermes(Hermes):
        async def dispatch(self, request: object):
            self.requests.append(request)
            return block(FactoryPhase.TOOL_CANDIDATE_TESTED, attempt=2).model_copy(
                update={"event_id": UUID("00000000-0000-0000-0000-000000000100")}
            )

    hermes, forge = RetryHermes(), Forge()

    action = await dispatcher(
        coordinator,
        hermes,
        forge,
        improvements=Improvements(),
    ).dispatch_next(factory_job.job_id)

    assert action.kind is FactoryActionKind.DISPATCH_TOOL_INTEGRATOR
    assert hermes.requests[0].improvement_authorization == authorization

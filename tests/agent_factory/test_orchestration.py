from __future__ import annotations

import pytest
from datetime import datetime, timezone
from uuid import UUID

from agenten.agent_factory.contracts import (
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.orchestration import FactoryDispatchError, FactoryDispatcher
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
    def __init__(self) -> None:
        self.requests = []

    async def submit(self, request: object) -> None:
        self.requests.append(request)


class CandidateValidator:
    def __init__(self) -> None:
        self.requests = []

    async def dispatch(self, request: object):
        self.requests.append(request)
        return block(FactoryPhase.BUILD_PASSED)


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
    hermes, forge = Hermes(), Forge()

    action = await dispatcher(coordinator, hermes, forge).dispatch_next(factory_job.job_id)

    assert action.kind is FactoryActionKind.SUBMIT_FORGE_JOB
    assert len(forge.requests) == 1


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


@pytest.mark.asyncio
async def test_quality_warden_uses_hermes_even_when_candidate_validator_exists() -> None:
    factory_job = job()

    class Coordinator:
        def __init__(self) -> None:
            self.recorded = []

        def next_action(self, _job_id):
            from agenten.agent_factory.state_machine import FactoryAction

            return FactoryAction(
                kind=FactoryActionKind.DISPATCH_QUALITY_WARDEN,
                attempt=1,
                job_id=factory_job.job_id,
            )

        def projection(self, _job_id):
            from types import SimpleNamespace

            return SimpleNamespace(job=factory_job)

        def record(self, evidence):
            self.recorded.append(evidence)

    class QualityHermes(Hermes):
        async def dispatch(self, request: object):
            self.requests.append(request)
            return block(FactoryPhase.QUALITY_REVIEWED)

    coordinator = Coordinator()
    hermes, forge, validator = QualityHermes(), Forge(), CandidateValidator()

    action = await dispatcher(coordinator, hermes, forge, validator).dispatch_next(
        factory_job.job_id
    )

    assert action.kind is FactoryActionKind.DISPATCH_QUALITY_WARDEN
    assert hermes.requests[0].role is FactoryRole.QUALITY_WARDEN
    assert validator.requests == []
    assert coordinator.recorded[0].phase is FactoryPhase.QUALITY_REVIEWED


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

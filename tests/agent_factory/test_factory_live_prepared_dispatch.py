from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.factory_live_prepared_dispatch import (
    FactoryLivePreparedDispatch,
    PreparedFactoryLiveEffectExecutor,
    PreparedFactoryLivePlan,
)
from agenten.agent_factory.factory_live_runner import (
    FactoryLiveEffectKind,
    FactoryLiveEffectOutcomeV1,
    FactoryLiveEffectRequestV1,
    FactoryLiveRunner,
    InMemoryFactoryLiveEffectLedger,
)
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.service import InMemoryFactoryRepository
from agenten.agent_factory.skill_workflow_contracts import FactorySkillInvocationV1
from agenten.agent_factory.state_machine import (
    FactoryAction,
    FactoryActionKind,
    FactoryProjection,
)
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_release_gate import workflow_job, workflow_run
from tests.agent_factory.test_skill_workflow_contracts import invocation_payload


NOW = datetime(2026, 7, 21, 10, 5, tzinfo=timezone.utc)
SKILL_DIGESTS = {"captain-factory-brief-codex": "a" * 64}


def artifact(name: str) -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://{name}",
        sha256=(name.encode("utf-8").hex() + "0" * 64)[:64],
        media_type="application/json",
    )


def codex_request(job) -> FactoryLiveEffectRequestV1:
    invocation = FactorySkillInvocationV1.model_validate(
        invocation_payload("brief_codex")
    ).model_copy(
        update={
            "lease": issue_factory_lease(
                job=job,
                role=FactoryRole.TOOL_INTEGRATOR,
                attempt=1,
                workspace_ref="workspace://factory/prepared-live",
                now=NOW,
            )
        }
    )
    return FactoryLiveEffectRequestV1(
        schema_name="captain.factory-live-effect-request.v1",
        effect_id=uuid5(NAMESPACE_URL, f"prepared-codex|{job.job_id}"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=1,
        kind=FactoryLiveEffectKind.CODEX,
        idempotency_key=invocation.idempotency_key,
        input_ref=invocation.input_ref,
        invocation=invocation,
    )


def provider_requests(job) -> tuple[FactoryLiveEffectRequestV1, ...]:
    runs = tuple(
        workflow_run(number)
        for number in range(1, job.execution_policy.required_live_runs + 1)
    )
    return tuple(
        FactoryLiveEffectRequestV1(
            schema_name="captain.factory-live-effect-request.v1",
            effect_id=uuid5(
                NAMESPACE_URL,
                f"prepared-provider|{job.job_id}|{run.run_number}",
            ),
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=1,
            kind=FactoryLiveEffectKind.PROVIDER,
            idempotency_key=run.invocation.idempotency_key,
            input_ref=run.invocation.input_ref,
            invocation=run.invocation.model_copy(
                update={
                    "lease": issue_factory_lease(
                        job=job,
                        role=FactoryRole.REAL_CASE_TESTER,
                        attempt=1,
                        workspace_ref="workspace://factory/prepared-live",
                        now=NOW,
                    )
                }
            ),
        )
        for run in runs
    )


def outcome(
    request: FactoryLiveEffectRequestV1,
) -> FactoryLiveEffectOutcomeV1:
    return FactoryLiveEffectOutcomeV1(
        schema_name="captain.factory-live-effect-outcome.v1",
        outcome_id=uuid5(NAMESPACE_URL, f"prepared-outcome|{request.effect_id}"),
        effect_id=request.effect_id,
        job_id=request.job_id,
        correlation_id=request.correlation_id,
        subject_version=request.subject_version,
        attempt=request.attempt,
        status="succeeded",
        evidence_ref=artifact(f"outcome-{request.effect_id}"),
        completed_at=NOW + timedelta(seconds=1),
    )


class ActionSource:
    def __init__(self, action: FactoryAction) -> None:
        self.action = action
        self.calls: list[UUID] = []

    def next_action(self, job_id: UUID) -> FactoryAction:
        self.calls.append(job_id)
        return self.action


class PreparedPort:
    def __init__(self, prepared: FactoryLivePreparedDispatch) -> None:
        self.prepared = prepared
        self.prepare_calls: list[dict[str, object]] = []
        self.execute_calls: list[FactoryLiveEffectRequestV1] = []
        self.recover_calls: list[FactoryLiveEffectRequestV1] = []
        self.execute_result = outcome(prepared.requests[0])
        self.recover_result: FactoryLiveEffectOutcomeV1 | None = outcome(
            prepared.requests[0]
        )

    def prepare(self, **values: object) -> FactoryLivePreparedDispatch:
        self.prepare_calls.append(values)
        return self.prepared

    async def execute(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectOutcomeV1:
        self.execute_calls.append(request)
        return self.execute_result

    async def recover(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectOutcomeV1 | None:
        self.recover_calls.append(request)
        return self.recover_result


def build_plan(job, action: FactoryAction, requests):
    prepared = FactoryLivePreparedDispatch(action=action, requests=requests)
    source = ActionSource(action)
    port = PreparedPort(prepared)
    plan = PreparedFactoryLivePlan(
        actions=source,
        dispatch=port,
        expected_skill_digests=SKILL_DIGESTS,
    )
    return plan, source, port


def test_plan_builds_requests_from_the_exact_gateway_action_and_typed_port() -> None:
    job = workflow_job(mode="demo")
    action = FactoryAction(
        kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
        attempt=1,
        job_id=job.job_id,
    )
    request = codex_request(job)
    plan, source, port = build_plan(job, action, (request,))
    projection = FactoryProjection.from_job(job)
    artifacts = (job.compiled_spec_ref,)

    planned = plan.effects_for(
        job=job,
        mode="demo",
        projection=projection,
        workflow_artifacts=artifacts,
    )

    assert planned == (request,)
    assert source.calls == [job.job_id]
    assert port.prepare_calls == [
        {
            "job": job,
            "action": action,
            "expected_skill_digests": SKILL_DIGESTS,
            "projection": projection,
            "workflow_artifacts": artifacts,
        }
    ]
    assert planned[0].invocation.lease.job_id == action.job_id
    assert planned[0].invocation.lease.attempt == action.attempt
    assert planned[0].idempotency_key == planned[0].invocation.idempotency_key


def test_prepared_dispatch_rejects_requests_for_a_different_action() -> None:
    job = workflow_job(mode="demo")
    request = codex_request(job)
    provider_action = FactoryAction(
        kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
        attempt=1,
        job_id=job.job_id,
    )

    with pytest.raises(ValueError, match="action"):
        FactoryLivePreparedDispatch(
            action=provider_action,
            requests=(request,),
        )


def test_plan_requires_the_exact_provider_request_count() -> None:
    job = workflow_job(mode="release")
    action = FactoryAction(
        kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
        attempt=1,
        job_id=job.job_id,
    )
    requests = provider_requests(job)
    plan, _, port = build_plan(job, action, requests)
    port.prepared = FactoryLivePreparedDispatch(
        action=action,
        requests=requests[:1],
    )

    with pytest.raises(ValueError, match="exact live request sequence"):
        plan.effects_for(
            job=job,
            mode="release",
            projection=FactoryProjection.from_job(job),
            workflow_artifacts=(),
        )


@pytest.mark.asyncio
async def test_executor_dispatches_only_the_exact_prepared_request() -> None:
    job = workflow_job(mode="demo")
    action = FactoryAction(
        kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
        attempt=1,
        job_id=job.job_id,
    )
    request = codex_request(job)
    plan, _, port = build_plan(job, action, (request,))
    plan.effects_for(
        job=job,
        mode="demo",
        projection=FactoryProjection.from_job(job),
        workflow_artifacts=(),
    )
    executor = PreparedFactoryLiveEffectExecutor(plan=plan, dispatch=port)

    observed = await executor.execute(request)

    assert observed == outcome(request)
    assert port.execute_calls == [request]
    changed = request.model_copy(
        update={"effect_id": uuid5(NAMESPACE_URL, "unprepared-effect")}
    )
    with pytest.raises(ValueError, match="exact prepared request"):
        await executor.execute(changed)
    assert port.execute_calls == [request]


@pytest.mark.asyncio
async def test_executor_recovery_never_starts_a_second_effect() -> None:
    job = workflow_job(mode="demo")
    action = FactoryAction(
        kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
        attempt=1,
        job_id=job.job_id,
    )
    request = codex_request(job)
    plan, _, port = build_plan(job, action, (request,))
    plan.effects_for(
        job=job,
        mode="demo",
        projection=FactoryProjection.from_job(job),
        workflow_artifacts=(),
    )
    executor = PreparedFactoryLiveEffectExecutor(plan=plan, dispatch=port)

    observed = await executor.recover(request)

    assert observed == outcome(request)
    assert port.recover_calls == [request]
    assert port.execute_calls == []


@pytest.mark.asyncio
async def test_release_plan_executes_the_runner_bound_requests_exactly_once() -> None:
    job = workflow_job(mode="release")
    action = FactoryAction(
        kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
        attempt=1,
        job_id=job.job_id,
    )
    requests = provider_requests(job)
    plan, _, port = build_plan(job, action, requests)

    async def execute_bound(request):
        port.execute_calls.append(request)
        return outcome(request)

    port.execute = execute_bound
    repository = InMemoryFactoryRepository()
    repository.register(job)
    runner = FactoryLiveRunner(
        repository=repository,
        effect_ledger=InMemoryFactoryLiveEffectLedger(),
        plan=plan,
        executor=PreparedFactoryLiveEffectExecutor(plan=plan, dispatch=port),
        clock=lambda: NOW,
    )

    report = await runner.run(job, mode="release")

    assert len(port.execute_calls) == job.execution_policy.required_live_runs
    assert len({request.run_id for request in port.execute_calls}) == 1
    assert tuple(request.run_effect_index for request in port.execute_calls) == (1, 2, 3)
    assert all(
        request.run_effect_count == job.execution_policy.required_live_runs
        for request in port.execute_calls
    )
    assert len(report.effects) == job.execution_policy.required_live_runs


@pytest.mark.asyncio
async def test_executor_rejects_an_outcome_for_another_effect() -> None:
    job = workflow_job(mode="demo")
    action = FactoryAction(
        kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
        attempt=1,
        job_id=job.job_id,
    )
    request = codex_request(job)
    plan, _, port = build_plan(job, action, (request,))
    plan.effects_for(
        job=job,
        mode="demo",
        projection=FactoryProjection.from_job(job),
        workflow_artifacts=(),
    )
    port.recover_result = outcome(request).model_copy(
        update={"effect_id": uuid5(NAMESPACE_URL, "foreign-outcome")}
    )
    executor = PreparedFactoryLiveEffectExecutor(plan=plan, dispatch=port)

    with pytest.raises(ValueError, match="outcome binding"):
        await executor.recover(request)

    assert port.recover_calls == [request]
    assert port.execute_calls == []


@pytest.mark.asyncio
async def test_executor_rejects_a_gateway_action_change_before_recovery() -> None:
    job = workflow_job(mode="demo")
    action = FactoryAction(
        kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
        attempt=1,
        job_id=job.job_id,
    )
    request = codex_request(job)
    plan, source, port = build_plan(job, action, (request,))
    plan.effects_for(
        job=job,
        mode="demo",
        projection=FactoryProjection.from_job(job),
        workflow_artifacts=(),
    )
    source.action = FactoryAction(
        kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
        attempt=1,
        job_id=job.job_id,
    )
    executor = PreparedFactoryLiveEffectExecutor(plan=plan, dispatch=port)

    with pytest.raises(ValueError, match="Gateway action changed"):
        await executor.recover(request)

    assert port.recover_calls == []
    assert port.execute_calls == []

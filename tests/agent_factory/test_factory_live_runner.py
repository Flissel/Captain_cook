from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agenten.agent_factory.factory_live_runner import (
    FactoryInfrastructureFailure,
    FactoryLiveBlockReason,
    FactoryLiveEffectKind,
    FactoryLiveEffectOutcomeV1,
    FactoryLiveEffectRequestV1,
    FactoryLiveRunReport,
    FactoryLiveRunner,
    InMemoryFactoryLiveEffectLedger,
)
from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.service import InMemoryFactoryRepository
from agenten.agent_factory.skill_workflow_contracts import FactorySkillInvocationV1
from agenten.agent_factory.state_machine import FactoryProjection
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_release_gate import (
    workflow_budget,
    workflow_evaluation,
    workflow_job,
    workflow_receipts,
    workflow_run,
)
from tests.agent_factory.test_skill_workflow_contracts import invocation_payload


NOW = datetime(2026, 7, 21, 10, 5, tzinfo=timezone.utc)


def artifact(name: str) -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://{name}",
        sha256=(name.encode("utf-8").hex() + "0" * 64)[:64],
        media_type="application/json",
    )


def effect_request(
    job,
    *,
    attempt: int = 1,
    kind: FactoryLiveEffectKind = FactoryLiveEffectKind.PROVIDER,
    key_char: str | None = None,
    now: datetime = NOW,
) -> FactoryLiveEffectRequestV1:
    effect_id = uuid5(
        NAMESPACE_URL,
        f"factory-live|{job.job_id}|{attempt}|{kind.value}",
    )
    step = "brief_codex" if kind is FactoryLiveEffectKind.CODEX else "execute_team"
    invocation = FactorySkillInvocationV1.model_validate(invocation_payload(step))
    role = (
        FactoryRole.TOOL_INTEGRATOR
        if kind is FactoryLiveEffectKind.CODEX
        else FactoryRole.REAL_CASE_TESTER
    )
    invocation = invocation.model_copy(
        update={
            "lease": issue_factory_lease(
                job=job,
                role=role,
                attempt=attempt,
                workspace_ref="workspace://factory/live-runner",
                now=now,
            )
        }
    )
    if key_char is not None:
        invocation = invocation.model_copy(
            update={
                "invocation_id": uuid5(
                    NAMESPACE_URL,
                    f"factory-live-invocation|{key_char}",
                ),
                "idempotency_key": key_char * 64,
            }
        )
    return FactoryLiveEffectRequestV1(
        schema_name="captain.factory-live-effect-request.v1",
        effect_id=effect_id,
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=attempt,
        kind=kind,
        idempotency_key=invocation.idempotency_key,
        input_ref=invocation.input_ref,
        invocation=invocation,
    )


def effect_outcome(
    request: FactoryLiveEffectRequestV1,
    *,
    status: str = "succeeded",
    reason: str | None = None,
) -> FactoryLiveEffectOutcomeV1:
    evidence_ref = (
        None
        if status in {reason.value for reason in FactoryLiveBlockReason}
        else artifact(f"effect-{request.effect_id}")
    )
    return FactoryLiveEffectOutcomeV1(
        schema_name="captain.factory-live-effect-outcome.v1",
        outcome_id=uuid5(NAMESPACE_URL, f"factory-live-outcome|{request.effect_id}"),
        effect_id=request.effect_id,
        job_id=request.job_id,
        correlation_id=request.correlation_id,
        subject_version=request.subject_version,
        attempt=request.attempt,
        status=status,
        evidence_ref=evidence_ref,
        reason=reason,
        completed_at=NOW + timedelta(seconds=1),
    )


def run_bound_request(
    request: FactoryLiveEffectRequestV1,
    *,
    run_id: UUID,
    run_effect_index: int,
    run_effect_count: int,
) -> FactoryLiveEffectRequestV1:
    payload = request.model_dump(mode="json", by_alias=True)
    payload.update(
        {
            "run_id": str(run_id),
            "run_effect_index": run_effect_index,
            "run_effect_count": run_effect_count,
        }
    )
    return FactoryLiveEffectRequestV1.model_validate(payload)


def distinct_effect_request(
    job,
    *,
    key_char: str,
    ordinal: int,
) -> FactoryLiveEffectRequestV1:
    request = effect_request(job, key_char=key_char)
    return FactoryLiveEffectRequestV1.model_validate(
        request.model_dump(mode="json", by_alias=True)
        | {
            "effect_id": str(
                uuid5(
                    NAMESPACE_URL,
                    f"factory-live|{job.job_id}|distinct|{ordinal}",
                )
            )
        }
    )


class Repository(InMemoryFactoryRepository):
    def __init__(self, job, *, artifacts=(), budget=None, receipts=()) -> None:
        super().__init__()
        self.register(job)
        self._artifacts = tuple(artifacts)
        self._budget = budget
        self._receipts = tuple(receipts)

    def workflow_artifacts(self, job_id: UUID):
        self.job(job_id)
        return self._artifacts

    def workflow_budget_projection(self, job_id: UUID):
        self.job(job_id)
        return self._budget

    def workflow_usage_receipts(self, job_id: UUID):
        self.job(job_id)
        return self._receipts


class Plan:
    def __init__(self, *effects: FactoryLiveEffectRequestV1) -> None:
        self._effects = effects

    def effects_for(self, *, job, mode, projection, workflow_artifacts):
        return self._effects


class Executor:
    def __init__(self, request, *, start, recover=None) -> None:
        self.request = request
        self.start_result = start
        self.recover_result = recover
        self.start_calls = 0
        self.recover_calls = 0

    async def execute(self, request):
        assert request == self.request
        self.start_calls += 1
        if isinstance(self.start_result, BaseException):
            raise self.start_result
        return self.start_result

    async def recover(self, request):
        assert request == self.request
        self.recover_calls += 1
        if isinstance(self.recover_result, BaseException):
            raise self.recover_result
        return self.recover_result


class MultiExecutor:
    def __init__(self) -> None:
        self.execute_calls = []

    async def execute(self, request):
        self.execute_calls.append(request)
        return effect_outcome(request)

    async def recover(self, request):
        raise AssertionError("recovery was not expected")


def test_effect_request_requires_exact_released_invocation_binding() -> None:
    job = workflow_job(mode="demo")
    request = effect_request(job)
    payload = request.model_dump(mode="json", by_alias=True)
    invocation = payload["invocation"]
    assert isinstance(invocation, dict)
    invocation["attempt"] = 2

    with pytest.raises(ValueError, match="attempt mismatch|invocation binding"):
        FactoryLiveEffectRequestV1.model_validate(payload)


def test_effect_claim_rejects_a_second_effect_id_for_the_same_idempotency_key() -> None:
    job = workflow_job(mode="demo")
    request = effect_request(job)
    ledger = InMemoryFactoryLiveEffectLedger()
    ledger.claim(request)
    duplicate_identity = request.model_copy(
        update={"effect_id": uuid5(NAMESPACE_URL, "different-effect-id")}
    )

    with pytest.raises(ValueError, match="idempotency_key"):
        ledger.claim(duplicate_identity)


def test_effect_claim_rejects_changed_identity_for_the_same_invocation() -> None:
    job = workflow_job(mode="demo")
    request = effect_request(job)
    ledger = InMemoryFactoryLiveEffectLedger()
    first = ledger.claim(request)
    replay = ledger.claim(request)
    changed_input = job.compiled_spec_ref
    changed_invocation = request.invocation.model_copy(
        update={
            "idempotency_key": "e" * 64,
            "input_ref": changed_input,
            "input_sha256": changed_input.sha256,
        }
    )
    changed = request.model_copy(
        update={
            "effect_id": uuid5(NAMESPACE_URL, "changed-invocation-effect-id"),
            "idempotency_key": changed_invocation.idempotency_key,
            "input_ref": changed_input,
            "invocation": changed_invocation,
        }
    )

    assert first.acquired is True
    assert replay.acquired is False
    with pytest.raises(ValueError, match="invocation_id"):
        ledger.claim(changed)


def test_generic_runner_cannot_claim_n8n_without_the_separate_runtime_path() -> None:
    with pytest.raises(ValueError, match="n8n"):
        FactoryLiveEffectKind("n8n")


def runner(job, ledger, plan, executor, *, repository=None) -> FactoryLiveRunner:
    return FactoryLiveRunner(
        repository=repository or Repository(job),
        effect_ledger=ledger,
        plan=plan,
        executor=executor,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_restart_after_reservation_recovers_without_starting_provider_twice() -> None:
    job = workflow_job(mode="demo")
    request = effect_request(job)
    ledger = InMemoryFactoryLiveEffectLedger()
    crashed = Executor(request, start=RuntimeError("process crashed"))

    with pytest.raises(RuntimeError, match="process crashed"):
        await runner(job, ledger, Plan(request), crashed).run(job.job_id, mode="demo")

    recovered = Executor(
        request,
        start=AssertionError("must not start again"),
        recover=effect_outcome(request),
    )
    report = await runner(job, ledger, Plan(request), recovered).run(
        job.job_id,
        mode="demo",
    )

    assert crashed.start_calls == 1
    assert recovered.start_calls == 0
    assert recovered.recover_calls == 1
    assert report.attempt == 1


def test_history_reconstructs_reserved_effect_after_process_crash() -> None:
    job = workflow_job(mode="demo")
    request = effect_request(job)
    ledger = InMemoryFactoryLiveEffectLedger()
    ledger.claim(request)

    restarted = runner(
        job,
        ledger,
        Plan(request),
        Executor(request, start=AssertionError("history must not dispatch")),
    )
    history = restarted.history(job.job_id)

    assert len(history) == 1
    assert isinstance(history[0], FactoryLiveRunReport)
    assert history[0].status == "infrastructure_recovery_required"
    assert history[0].attempt == 1
    assert history[0].next_attempt == 1
    assert history[0].effects[0].effect_id == request.effect_id
    assert history[0].effects[0].status == "reserved"
    assert history[0].effects[0].provider_started is None
    assert history[0].effects[0].evidence_ref is None
    assert history[0].effects[0].reason == (
        "reserved external effect requires authoritative recovery evidence"
    )


@pytest.mark.asyncio
async def test_runner_binds_unbound_planned_effects_as_one_deterministic_run() -> None:
    job = workflow_job(mode="release")
    first = distinct_effect_request(job, key_char="c", ordinal=1)
    second = distinct_effect_request(job, key_char="d", ordinal=2)
    ledger = InMemoryFactoryLiveEffectLedger()

    await runner(job, ledger, Plan(first, second), MultiExecutor()).run(
        job,
        mode="release",
    )

    requests = tuple(record.request for record in ledger.history(job.job_id))
    assert len(requests) == 2
    assert requests[0].run_id is not None
    assert requests[1].run_id == requests[0].run_id
    assert tuple(request.run_effect_index for request in requests) == (1, 2)
    assert tuple(request.run_effect_count for request in requests) == (2, 2)


@pytest.mark.asyncio
async def test_release_runner_rejects_inconsistent_bound_plan_before_claim() -> None:
    job = workflow_job(mode="release")
    first = run_bound_request(
        distinct_effect_request(job, key_char="c", ordinal=1),
        run_id=uuid5(NAMESPACE_URL, f"factory-live-invalid-a|{job.job_id}"),
        run_effect_index=1,
        run_effect_count=2,
    )
    second = run_bound_request(
        distinct_effect_request(job, key_char="d", ordinal=2),
        run_id=uuid5(NAMESPACE_URL, f"factory-live-invalid-b|{job.job_id}"),
        run_effect_index=2,
        run_effect_count=2,
    )
    ledger = InMemoryFactoryLiveEffectLedger()

    with pytest.raises(ValueError, match="run binding mismatch"):
        await runner(job, ledger, Plan(first, second), MultiExecutor()).run(
            job,
            mode="release",
        )

    assert ledger.history(job.job_id) == ()


@pytest.mark.asyncio
async def test_release_runner_preflights_every_request_before_first_claim() -> None:
    job = workflow_job(mode="release")
    first = distinct_effect_request(job, key_char="c", ordinal=1)
    foreign_job = job.model_copy(
        update={
            "job_id": uuid5(NAMESPACE_URL, f"factory-live-foreign-job|{job.job_id}"),
            "correlation_id": uuid5(
                NAMESPACE_URL,
                f"factory-live-foreign-correlation|{job.job_id}",
            ),
        }
    )
    second = distinct_effect_request(job, key_char="d", ordinal=2)
    foreign_lease = issue_factory_lease(
        job=foreign_job,
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        workspace_ref="workspace://factory/live-runner",
        now=NOW,
    )
    payload = second.model_dump(mode="json", by_alias=True)
    payload["job_id"] = str(foreign_job.job_id)
    payload["correlation_id"] = str(foreign_job.correlation_id)
    payload["invocation"]["job_id"] = str(foreign_job.job_id)
    payload["invocation"]["correlation_id"] = str(foreign_job.correlation_id)
    payload["invocation"]["lease"] = foreign_lease.model_dump(
        mode="json",
        by_alias=True,
    )
    invalid_second = FactoryLiveEffectRequestV1.model_validate(payload)
    ledger = InMemoryFactoryLiveEffectLedger()
    executor = MultiExecutor()

    with pytest.raises(ValueError, match="does not match Gateway projection"):
        await runner(
            job,
            ledger,
            Plan(first, invalid_second),
            executor,
        ).run(job, mode="release")

    assert ledger.history(job.job_id) == ()
    assert executor.execute_calls == []


@pytest.mark.asyncio
async def test_prefix_crash_before_next_claim_is_recoverable() -> None:
    job = workflow_job(mode="release")
    run_id = uuid5(NAMESPACE_URL, f"factory-live-prefix|{job.job_id}")
    first = run_bound_request(
        distinct_effect_request(job, key_char="c", ordinal=1),
        run_id=run_id,
        run_effect_index=1,
        run_effect_count=2,
    )
    second = run_bound_request(
        distinct_effect_request(job, key_char="d", ordinal=2),
        run_id=run_id,
        run_effect_index=2,
        run_effect_count=2,
    )
    ledger = InMemoryFactoryLiveEffectLedger()
    ledger.claim(first)
    ledger.complete(first, effect_outcome(first))
    restarted = runner(job, ledger, Plan(first, second), MultiExecutor())

    prefix_history = restarted.history(job.job_id)
    resumed = await restarted.run(job, mode="release")
    final_history = runner(
        job,
        ledger,
        Plan(),
        MultiExecutor(),
    ).history(job.job_id)

    assert prefix_history[0].status == "infrastructure_recovery_required"
    assert tuple(effect.effect_id for effect in resumed.effects) == (
        first.effect_id,
        second.effect_id,
    )
    assert tuple(effect.effect_id for effect in final_history[-1].effects) == (
        first.effect_id,
        second.effect_id,
    )


def test_history_release_decisions_do_not_see_future_run_evidence() -> None:
    job = workflow_job(mode="release")
    runs = tuple(workflow_run(number) for number in range(1, 4))
    requests = tuple(
        run_bound_request(
            FactoryLiveEffectRequestV1.model_validate(
                effect_request(job, key_char=key_char).model_dump(
                    mode="json", by_alias=True
                )
                | {
                    "effect_id": str(
                        uuid5(
                            NAMESPACE_URL,
                            f"factory-live-history-run|{job.job_id}|{number}",
                        )
                    ),
                    "idempotency_key": run.invocation.idempotency_key,
                    "input_ref": run.invocation.input_ref.model_dump(mode="json"),
                    "invocation": run.invocation.model_dump(
                        mode="json", by_alias=True
                    ),
                }
            ),
            run_id=uuid5(
                NAMESPACE_URL,
                f"factory-live-history-group|{job.job_id}|{number}",
            ),
            run_effect_index=1,
            run_effect_count=1,
        )
        for number, (run, key_char) in enumerate(
            zip(runs, ("c", "d", "e"), strict=True),
            start=1,
        )
    )
    ledger = InMemoryFactoryLiveEffectLedger()
    for request in requests:
        ledger.claim(request)
        ledger.complete(request, effect_outcome(request))
    repository = Repository(
        job,
        artifacts=(*runs, workflow_evaluation(runs)),
        budget=workflow_budget(),
        receipts=workflow_receipts(runs),
    )

    history = runner(
        job,
        ledger,
        Plan(),
        MultiExecutor(),
        repository=repository,
    ).history(job.job_id)

    assert tuple(report.status for report in history) == (
        "blocked",
        "blocked",
        "ready",
    )
    assert "missing exactly three" in history[0].reasons[0]
    assert "missing exactly three" in history[1].reasons[0]


@pytest.mark.asyncio
async def test_history_after_recovery_is_durable_and_exact_replay_is_not_duplicated() -> None:
    job = workflow_job(mode="demo")
    first = effect_request(job, kind=FactoryLiveEffectKind.CODEX, key_char="c")
    second = effect_request(job, kind=FactoryLiveEffectKind.PROVIDER, key_char="d")
    ledger = InMemoryFactoryLiveEffectLedger()
    ledger.claim(first)
    ledger.complete(first, effect_outcome(first))
    ledger.claim(second)

    recovered = Executor(
        second,
        start=AssertionError("must recover"),
        recover=effect_outcome(second),
    )
    await runner(job, ledger, Plan(second), recovered).run(job, mode="demo")
    await runner(
        job,
        ledger,
        Plan(second),
        Executor(
            second,
            start=AssertionError("must not start"),
            recover=AssertionError("must not recover"),
        ),
    ).run(job, mode="demo")

    history = ledger.history(job.job_id)
    assert tuple(record.request.effect_id for record in history) == (
        first.effect_id,
        second.effect_id,
    )
    assert all(record.outcome is not None for record in history)
    assert recovered.recover_calls == 1
    rebuilt = runner(
        job,
        ledger,
        Plan(),
        Executor(second, start=AssertionError("history must not execute")),
    ).history(job.job_id)
    assert all(isinstance(report, FactoryLiveRunReport) for report in rebuilt)
    assert tuple(report.status for report in rebuilt[-2:]) == (
        "infrastructure_recovery_required",
        "blocked",
    )
    assert rebuilt[-1].effects[0].effect_id == second.effect_id
    assert rebuilt[-1].effects[0].completion_origin == "recover"


@pytest.mark.asyncio
async def test_recovered_multi_effect_run_survives_a_second_restart_with_grouping() -> None:
    job = workflow_job(mode="demo")
    run_id = uuid5(NAMESPACE_URL, f"factory-live-run|{job.job_id}|grouped")
    first = run_bound_request(
        effect_request(job, kind=FactoryLiveEffectKind.CODEX, key_char="c"),
        run_id=run_id,
        run_effect_index=1,
        run_effect_count=2,
    )
    second = run_bound_request(
        effect_request(job, kind=FactoryLiveEffectKind.CODEX, key_char="d").model_copy(
            update={
                "effect_id": uuid5(
                    NAMESPACE_URL,
                    f"factory-live|{job.job_id}|grouped|second",
                )
            }
        ),
        run_id=run_id,
        run_effect_index=2,
        run_effect_count=2,
    )
    ledger = InMemoryFactoryLiveEffectLedger()
    ledger.claim(first)
    ledger.complete(first, effect_outcome(first))
    ledger.claim(second)

    before_recovery = runner(
        job,
        ledger,
        Plan(first, second),
        Executor(
            second,
            start=AssertionError("must recover"),
            recover=effect_outcome(second),
        ),
    )
    open_history = before_recovery.history(job.job_id)
    assert len(open_history) == 1
    assert open_history[0].status == "infrastructure_recovery_required"
    assert tuple(effect.effect_id for effect in open_history[0].effects) == (
        first.effect_id,
        second.effect_id,
    )

    recovered = await before_recovery.run(job, mode="demo")
    assert recovered.effects[-1].completion_origin == "recover"

    after_second_restart = runner(
        job,
        ledger,
        Plan(),
        Executor(second, start=AssertionError("history must not execute")),
    ).history(job.job_id)
    assert tuple(report.status for report in after_second_restart[:-1]) == (
        "infrastructure_recovery_required",
    )
    assert tuple(effect.effect_id for effect in after_second_restart[-1].effects) == (
        first.effect_id,
        second.effect_id,
    )
    assert tuple(
        effect.completion_origin for effect in after_second_restart[-1].effects
    ) == ("execute", "recover")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", tuple(FactoryLiveEffectKind))
async def test_restart_after_evidence_replays_without_any_external_effect(
    kind: FactoryLiveEffectKind,
) -> None:
    job = workflow_job(mode="demo")
    request = effect_request(job, kind=kind)
    ledger = InMemoryFactoryLiveEffectLedger()
    first = Executor(request, start=effect_outcome(request))
    await runner(job, ledger, Plan(request), first).run(job, mode="demo")

    replay = Executor(
        request,
        start=AssertionError("must not start"),
        recover=AssertionError("must not recover"),
    )
    report = await runner(job, ledger, Plan(request), replay).run(job, mode="demo")

    assert first.start_calls == 1
    assert replay.start_calls == 0
    assert replay.recover_calls == 0
    assert report.effects[0].replayed is True


@pytest.mark.asyncio
async def test_behavioral_retry_advances_attempt_but_infrastructure_recovery_does_not() -> None:
    job = workflow_job(mode="demo")
    request = effect_request(job)
    behavioral = Executor(
        request,
        start=effect_outcome(request, status="behavioral_failure"),
    )
    behavioral_report = await runner(
        job,
        InMemoryFactoryLiveEffectLedger(),
        Plan(request),
        behavioral,
    ).run(job, mode="demo")

    infrastructure = Executor(
        request,
        start=FactoryInfrastructureFailure("provider unavailable"),
    )
    infrastructure_report = await runner(
        job,
        InMemoryFactoryLiveEffectLedger(),
        Plan(request),
        infrastructure,
    ).run(job, mode="demo")

    assert behavioral_report.status == "behavioral_retry_required"
    assert behavioral_report.next_attempt == 2
    assert infrastructure_report.status == "infrastructure_recovery_required"
    assert infrastructure_report.attempt == 1
    assert infrastructure_report.next_attempt == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block_reason",
    tuple(FactoryLiveBlockReason),
)
async def test_non_dispatched_blocks_are_distinct_and_do_not_consume_attempt(
    block_reason: FactoryLiveBlockReason,
) -> None:
    job = workflow_job(mode="demo")
    request = effect_request(job)
    ledger = InMemoryFactoryLiveEffectLedger()

    class PreDispatchExecutor:
        provider_calls = 0

        async def execute(self, supplied):
            assert supplied == request
            return effect_outcome(
                request,
                status=block_reason.value,
                reason=f"blocked by {block_reason.value}",
            )

        async def recover(self, supplied):
            raise AssertionError("non-dispatched block must replay from the ledger")

    executor = PreDispatchExecutor()
    report = await runner(job, ledger, Plan(request), executor).run(job, mode="demo")

    assert report.status == "blocked"
    assert report.attempt == 1
    assert report.next_attempt == 1
    assert report.reasons == (f"blocked by {block_reason.value}",)
    assert report.effects[0].status == block_reason.value
    assert report.effects[0].provider_started is False
    assert executor.provider_calls == 0
    assert ledger.history(job.job_id)[0].outcome == effect_outcome(
        request,
        status=block_reason.value,
        reason=f"blocked by {block_reason.value}",
    )


def test_non_dispatched_block_requires_an_exact_reason_and_no_effect_evidence() -> None:
    job = workflow_job(mode="demo")
    request = effect_request(job)
    payload = effect_outcome(request).model_dump(mode="json", by_alias=True)
    payload.update({"status": "credential_required", "reason": None})

    with pytest.raises(ValueError, match="reason"):
        FactoryLiveEffectOutcomeV1.model_validate(payload)

    payload.update(
        {
            "reason": "credential is required",
            "evidence_ref": artifact("must-not-be-provider-evidence").model_dump(
                mode="json"
            ),
        }
    )
    with pytest.raises(ValueError, match="evidence"):
        FactoryLiveEffectOutcomeV1.model_validate(payload)


def test_historical_success_outcome_without_new_optional_fields_stays_readable() -> None:
    job = workflow_job(mode="demo")
    request = effect_request(job)
    old_payload = effect_outcome(request).model_dump(mode="json", by_alias=True)
    old_payload.pop("reason")
    old_payload.pop("completion_origin")

    restored = FactoryLiveEffectOutcomeV1.model_validate(old_payload)

    assert restored.status == "succeeded"
    assert restored.reason is None
    assert restored.completion_origin == "execute"


@pytest.mark.asyncio
async def test_runner_revalidates_deadline_immediately_before_each_new_effect() -> None:
    job = workflow_job(mode="demo")
    first = effect_request(job, kind=FactoryLiveEffectKind.CODEX, key_char="c")
    second = effect_request(job, kind=FactoryLiveEffectKind.PROVIDER, key_char="d")
    moments = iter(
        (
            NOW,
            NOW,
            job.deadline_at,
        )
    )
    executor = MultiExecutor()
    live_runner = FactoryLiveRunner(
        repository=Repository(job),
        effect_ledger=InMemoryFactoryLiveEffectLedger(),
        plan=Plan(first, second),
        executor=executor,
        clock=lambda: next(moments),
    )

    with pytest.raises(ValueError, match="active JobV3 deadline"):
        await live_runner.run(job, mode="demo")

    assert executor.execute_calls == [first]


def test_behavioral_failure_at_iteration_ceiling_is_blocked() -> None:
    job = workflow_job(mode="demo")
    projection = FactoryProjection.from_job(job).model_copy(update={"attempt": 5})

    report = FactoryLiveRunner._behavioral_report(
        job,
        "demo",
        projection,
        [],
    )

    assert report.status == "blocked"
    assert report.next_attempt == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "run_count", "expected"),
    (("demo", 1, "demo_ready"), ("release", 3, "ready")),
)
async def test_report_preserves_demo_ready_and_never_claims_ready_to_use(
    mode: str,
    run_count: int,
    expected: str,
) -> None:
    job = workflow_job(mode=mode)
    runs = tuple(workflow_run(number) for number in range(1, run_count + 1))
    repository = Repository(
        job,
        artifacts=(*runs, workflow_evaluation(runs)),
        budget=workflow_budget(),
        receipts=workflow_receipts(runs),
    )
    executor = Executor(effect_request(job), start=AssertionError("no effect planned"))

    report = await runner(
        job,
        InMemoryFactoryLiveEffectLedger(),
        Plan(),
        executor,
        repository=repository,
    ).run(job, mode=mode)

    assert report.status == expected
    assert report.release_decision is not None
    assert report.release_decision.status == expected
    assert "ready_to_use" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_runner_rejects_mode_that_does_not_match_captain_job() -> None:
    job = workflow_job(mode="demo")
    executor = Executor(effect_request(job), start=AssertionError("not authorized"))

    with pytest.raises(ValueError, match="mode does not match"):
        await runner(
            job,
            InMemoryFactoryLiveEffectLedger(),
            Plan(),
            executor,
        ).run(job, mode="release")

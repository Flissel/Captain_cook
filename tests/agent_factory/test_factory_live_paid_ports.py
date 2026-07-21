from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from agenten.agent_factory.capability_live_adapters import ContentAddressedArtifactStore
from agenten.agent_factory.contracts import (
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.execution_budget import BudgetExhausted
from agenten.agent_factory.factory_live_paid_ports import (
    DurableFactoryLivePreparedDispatch,
    build_factory_live_runtime,
)
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_sequence import FactoryImprovementAuthorizationV1
from agenten.agent_factory.skill_workflow_contracts import (
    FACTORY_SKILL_ID_BY_STEP,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind, FactoryProjection
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_release_gate import workflow_job
from tests.agent_factory.test_skill_workflow_contracts import (
    evaluation_payload,
    execution_payload,
)


class Catalog:
    def released_for(self, job, step):
        digest = "a" * 64
        return ReleasedHermesSkill(
            schema_name="captain.released-hermes-skill.v1",
            skill_id=FACTORY_SKILL_ID_BY_STEP[step],
            version=1,
            capability=job.required_capability,
            content_ref=ArtifactRef(
                uri=f"artifact://released/{digest}",
                sha256=digest,
                media_type="application/json",
            ),
            content_sha256=digest,
            status="released",
            released_at=job.occurred_at,
            producer="captain",
        )


class Leases:
    def __init__(self, now):
        self.now = now

    def active(self, job, role, attempt, _now):
        return issue_factory_lease(
            job=job,
            role=role,
            attempt=attempt,
            workspace_ref="workspace://factory/live-paid",
            now=self.now,
        )


class ExhaustedEffects:
    def __init__(self):
        self.calls = 0

    async def execute(self, *, job, request):
        assert job.job_id == request.job_id
        self.calls += 1
        raise BudgetExhausted("released budget exhausted")


class MissingHermesEffects:
    async def execute(self, *, job, request):
        raise FactoryDispatchError("Hermes CLI executable is unavailable")


def prepared(tmp_path: Path):
    job = workflow_job(mode="demo")
    now = job.occurred_at + timedelta(minutes=1)
    effects = ExhaustedEffects()
    port = DurableFactoryLivePreparedDispatch(
        job=job,
        released_skills=Catalog(),
        leases=Leases(now),
        effects=effects,
        artifacts=ContentAddressedArtifactStore(tmp_path / "cas"),
        clock=lambda: now,
    )
    action = FactoryAction(
        kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
        attempt=1,
        job_id=job.job_id,
    )
    batch = port.prepare(
        job=job,
        action=action,
        expected_skill_digests={"captain-factory-brief-codex": "a" * 64},
        projection=FactoryProjection.from_job(job),
        workflow_artifacts=(),
    )
    return job, now, effects, port, action, batch


def test_prepared_dispatch_builds_exact_released_skill_request(tmp_path: Path) -> None:
    job, _now, _effects, _port, action, batch = prepared(tmp_path)

    assert batch.action == action
    assert len(batch.requests) == 1
    request = batch.requests[0]
    assert request.job_id == job.job_id
    assert request.kind.value == "codex"
    assert request.invocation.step.value == "brief_codex"
    assert request.invocation.released_skill.content_sha256 == "a" * 64
    assert request.idempotency_key == request.invocation.idempotency_key


def test_release_provider_batch_has_three_distinct_restart_stable_runs(tmp_path: Path) -> None:
    job = workflow_job(mode="release")
    now = job.occurred_at + timedelta(minutes=1)
    port = DurableFactoryLivePreparedDispatch(
        job=job,
        released_skills=Catalog(),
        leases=Leases(now),
        effects=ExhaustedEffects(),
        artifacts=ContentAddressedArtifactStore(tmp_path / "cas"),
        clock=lambda: now,
    )
    action = FactoryAction(
        kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
        attempt=1,
        job_id=job.job_id,
    )

    batch = port.prepare(
        job=job,
        action=action,
        expected_skill_digests={"captain-factory-execute-team": "a" * 64},
        projection=FactoryProjection.from_job(job),
        workflow_artifacts=(),
    )

    assert len(batch.requests) == job.execution_policy.required_live_runs == 3
    assert len({request.effect_id for request in batch.requests}) == 3
    assert len({request.idempotency_key for request in batch.requests}) == 3
    assert len({request.run_id for request in batch.requests}) == 1
    assert tuple(request.run_effect_index for request in batch.requests) == (1, 2, 3)
    assert all(request.run_effect_count == 3 for request in batch.requests)


@pytest.mark.asyncio
async def test_budget_block_is_durable_and_recovery_never_restarts_effect(tmp_path: Path) -> None:
    job, now, effects, first, action, batch = prepared(tmp_path)
    request = batch.requests[0]

    outcome = await first.execute(request)
    assert outcome.status == "budget_exhausted"
    assert outcome.evidence_ref is None
    assert effects.calls == 1

    restarted_effects = ExhaustedEffects()
    restarted = DurableFactoryLivePreparedDispatch(
        job=job,
        released_skills=Catalog(),
        leases=Leases(now),
        effects=restarted_effects,
        artifacts=ContentAddressedArtifactStore(tmp_path / "cas"),
        clock=lambda: now,
    )
    replay = restarted.prepare(
        job=job,
        action=action,
        expected_skill_digests={"captain-factory-brief-codex": "a" * 64},
        projection=FactoryProjection.from_job(job),
        workflow_artifacts=(),
    )
    recovered = await restarted.recover(replay.requests[0])

    assert recovered is not None
    assert recovered.status == "budget_exhausted"
    assert recovered.completion_origin == "recover"
    assert restarted_effects.calls == 0


@pytest.mark.asyncio
async def test_missing_hermes_cli_is_a_non_dispatched_required_tool(tmp_path: Path) -> None:
    job, now, _effects, _first, action, _batch = prepared(tmp_path)
    port = DurableFactoryLivePreparedDispatch(
        job=job,
        released_skills=Catalog(),
        leases=Leases(now),
        effects=MissingHermesEffects(),
        artifacts=ContentAddressedArtifactStore(tmp_path / "missing-tool-cas"),
        clock=lambda: now,
    )
    batch = port.prepare(
        job=job,
        action=action,
        expected_skill_digests={"captain-factory-brief-codex": "a" * 64},
        projection=FactoryProjection.from_job(job),
        workflow_artifacts=(),
    )

    outcome = await port.execute(batch.requests[0])

    assert outcome.status == "required_tool"
    assert outcome.evidence_ref is None
    assert outcome.reason == "Hermes CLI is unavailable"


def test_production_builder_needs_no_manual_dependency_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = workflow_job(mode="demo")
    monkeypatch.setenv("CAPTAIN_FACTORY_ARTIFACT_ROOT", str(tmp_path / "factory-cas"))
    composition = SimpleNamespace(
        job=job,
        repository=Catalog(),
        leases=object(),
        budget=object(),
        workflow_sink=object(),
        skill_digests={"captain-factory-brief-codex": "a" * 64},
    )
    context = SimpleNamespace(
        composition=composition,
        runtime_url="http://127.0.0.1:8091",
        runtime_token=SecretStr("runtime-token"),
    )

    graph = build_factory_live_runtime(context)

    assert callable(graph.prepared_dispatch.prepare)
    assert callable(graph.prepared_dispatch.execute)
    assert callable(graph.prepared_dispatch.recover)
    assert callable(graph.materializer.validate_next)
    assert callable(graph.materializer.dispatch_next)
    assert (tmp_path / "factory-cas" / "content" / "sha256").is_dir()


def test_improvement_request_binds_real_prior_candidate_for_valid_retry() -> None:
    from agenten.agent_factory.factory_live_entrypoint import _known_improvement_refs

    execution = TeamExecutionEvidenceV1.model_validate(execution_payload())
    evaluation_data = evaluation_payload(
        failure_class="behavioral_failure",
        recommendation="RETRY_BUILD",
    )
    outcomes = evaluation_data["assertion_outcomes"]
    assert isinstance(outcomes, list)
    outcomes[1] = {**outcomes[1], "status": "failed"}
    evaluation = TeamEvaluationV1.model_validate(evaluation_data)
    projection = SimpleNamespace(
        attempt=1,
        workflow_evaluation_ref=evaluation.artifact_ref,
        feedback_ref=None,
    )
    references = _known_improvement_refs(projection, (execution, evaluation))
    block = FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id="00000000-0000-0000-0000-000000000901",
        job_id=evaluation.job_id,
        correlation_id=evaluation.correlation_id,
        occurred_at=evaluation.occurred_at + timedelta(seconds=1),
        producer="captain",
        subject_version=evaluation.subject_version,
        attempt=1,
        phase=FactoryPhase.IMPROVEMENT_REQUESTED,
        status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=references,
        evidence_refs=references,
    )

    authorization = FactoryImprovementAuthorizationV1(
        schema_name="captain.factory-improvement-authorization.v1",
        authorization_ref=evaluation.artifact_ref,
        authorized_attempt=2,
        request_block=block,
        failed_evaluation=evaluation,
        prior_candidate_ref=execution.candidate_ref,
        prior_green_assertion_ids=evaluation.prior_green_regression_ids,
    )

    assert execution.candidate_ref in block.artifact_refs
    assert authorization.prior_candidate_ref == execution.candidate_ref

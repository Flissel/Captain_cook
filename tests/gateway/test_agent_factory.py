from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agenten.agent_factory.contracts import AgentFactoryJob, FactoryPhase, FactoryRole
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.release_gate import (
    E2EKind,
    E2EOutcome,
    E2ERunEvidence,
    evaluate_factory_release,
)
from agenten.agent_factory.skill_evaluation import HermesSkillEvaluationEvidence
from agenten.agent_factory.execution_budget import (
    FactoryUsageReceiptV1,
    InMemoryFactoryBudgetLedger,
)
from agenten.agent_factory.business_benchmark_contracts import (
    canonical_business_benchmark_model_bytes,
)
from agenten.agent_runtime.contracts import ArtifactRef
from blockchain.mariadb_storage import MariaDBStorage
from gateway.app import create_app
from gateway.auth import GatewayRole, require_actor
from gateway.contracts import (
    FactorySkillEvaluationSubmission,
    FactoryReleaseDecisionSubmission,
    FactoryUsageSubmissionV2,
    PublishedHermesSkill,
)
from gateway.settings import GatewaySettings
from gateway.store import GatewayStore
from tests.agent_factory.test_state_machine import block, job
from tests.agent_factory.test_skill_evaluation_contracts import evidence_payload
from tests.agent_factory.test_execution_budget import job_v3, usage_payload
from tests.gateway.test_factory_budget import record_usage_lease
from tests.agent_factory.test_business_benchmark_contracts import summary as business_summary
from tests.agent_factory.test_release_gate import (
    workflow_benchmark,
    workflow_job,
    workflow_run,
)
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")


class Mirror:
    def enqueue_nowait(self, _: dict[str, object]) -> None:
        return None


async def captain_actor(_: Request) -> GatewayRole:
    return GatewayRole.CAPTAIN


def application(
    storage: MariaDBStorage,
    *,
    actor: GatewayRole = GatewayRole.CAPTAIN,
) -> FastAPI:
    assert TEST_DSN is not None
    app = create_app(
        storage=storage,
        mirror=Mirror(),
        settings=GatewaySettings(
            ledger_dsn=SecretStr(TEST_DSN),
            captain_gateway_token=SecretStr("captain-test-token"),
            worker_gateway_token=SecretStr("worker-test-token"),
        ),
    )
    async def selected_actor(_: Request) -> GatewayRole:
        return actor

    app.dependency_overrides[require_actor] = selected_actor
    return app


@pytest.fixture
def storage() -> MariaDBStorage:
    assert TEST_DSN is not None
    assert_isolated_test_database(TEST_DSN)
    value = MariaDBStorage(TEST_DSN)
    value.clear()
    yield value
    value.clear()


def test_factory_job_and_block_are_idempotent_and_restart_safe(storage: MariaDBStorage) -> None:
    factory_job = job()
    forge = block(FactoryPhase.FORGE_REQUESTED)
    with TestClient(application(storage)) as client:
        first = client.post("/v1/factory/jobs", json=factory_job.model_dump(mode="json", by_alias=True))
        replay = client.post("/v1/factory/jobs", json=factory_job.model_dump(mode="json", by_alias=True))
        assert first.status_code == replay.status_code == 202
        assert first.json()["replayed"] is False
        assert replay.json()["replayed"] is True
        assert client.post("/v1/factory/blocks", json=forge.model_dump(mode="json", by_alias=True)).status_code == 201

    with TestClient(application(storage)) as restarted:
        recovered = restarted.get(f"/v1/factory/jobs/{factory_job.job_id}")

    assert recovered.status_code == 200
    assert recovered.json()["projection"]["phase"] == "forge_requested"
    assert [item["phase"] for item in recovered.json()["blocks"]] == ["forge_requested"]


def _seed_workflow_execution(store: GatewayStore, execution) -> None:
    canonical = execution.model_dump(mode="json", by_alias=True)
    digest = store._canonical_model_sha256(execution)
    with store.storage.transaction() as connection:
        with connection.cursor() as cursor:
            job_block = store._runtime_block_by_json_value(
                cursor,
                block_type="agent_factory_job",
                field="job_id",
                value=str(execution.job_id),
                for_update=True,
            )
            assert job_block is not None
            index = store._next_index(cursor)
            block = store._new_block(
                cursor,
                index=index,
                block_type="factory_workflow_artifact",
                data=canonical,
                status="accepted",
                parent_index=job_block["index"],
                metadata={"schema": execution.schema_name},
            )
            store._insert(cursor, block)
            cursor.execute(
                """INSERT INTO factory_workflow_artifacts
                   (invocation_id, job_id, correlation_id, subject_version, attempt,
                    schema_name, content_sha256, block_index, payload)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    str(execution.invocation_id), str(execution.job_id),
                    str(execution.correlation_id), execution.subject_version,
                    execution.attempt, execution.schema_name, digest, index,
                    json.dumps(canonical, sort_keys=True),
                ),
            )


def test_business_benchmark_summary_is_restart_safe_and_rejects_changed_replay(
    storage: MariaDBStorage,
) -> None:
    factory_job = workflow_job(mode="demo")
    execution = workflow_run(1)
    summary = workflow_benchmark((execution,))
    store = GatewayStore(storage)
    store.record_factory_job(factory_job)
    _seed_workflow_execution(store, execution)
    payload = summary.model_dump(mode="json", by_alias=True)

    with TestClient(application(storage)) as captain:
        first = captain.post("/v1/factory/business-benchmarks", json=payload)
        replay = captain.post("/v1/factory/business-benchmarks", json=payload)
    with TestClient(application(storage, actor=GatewayRole.WORKER)) as hermes:
        forbidden = hermes.post("/v1/factory/business-benchmarks", json=payload)

    changed_payload = summary.model_dump(mode="json", by_alias=True)
    changed_payload.pop("artifact_ref")
    changed_payload["summary_id"] = "00000000-0000-0000-0000-000000000999"
    changed_payload["evaluated_at"] = (
        summary.evaluated_at + timedelta(seconds=1)
    )
    changed = business_summary(**changed_payload)
    with TestClient(application(storage)) as restarted:
        conflict = restarted.post(
            "/v1/factory/business-benchmarks",
            json=changed.model_dump(mode="json", by_alias=True),
        )
        by_id = restarted.get(
            f"/v1/factory/business-benchmarks/{summary.summary_id}"
        )
        by_artifact = restarted.get(
            "/v1/factory/business-benchmarks/artifacts/"
            f"{summary.artifact_ref.sha256}"
        )
        events = restarted.get(
            f"/v1/projects/agent-factory/runs/{factory_job.job_id}/events"
        )

    assert first.status_code == 201
    assert first.json()["artifact_sha256"] == summary.artifact_ref.sha256
    assert first.json()["content_sha256"] == hashlib.sha256(
        canonical_business_benchmark_model_bytes(summary)
    ).hexdigest()
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert forbidden.status_code == 403
    assert conflict.status_code == 409
    assert by_id.status_code == by_artifact.status_code == 200
    assert by_id.json() == by_artifact.json() == payload
    assert [event["event_type"] for event in events.json()] == [
        "captain_business_benchmark_validated"
    ]


def _second_workflow_job_and_execution():
    first_job = workflow_job(mode="demo")
    first_execution = workflow_run(1)
    second_job_id = UUID("00000000-0000-0000-0000-000000000901")
    second_correlation_id = UUID("00000000-0000-0000-0000-000000000902")
    second_invocation_id = UUID("00000000-0000-0000-0000-000000000921")
    second_job = first_job.model_copy(
        update={
            "event_id": UUID("00000000-0000-0000-0000-000000000911"),
            "job_id": second_job_id,
            "correlation_id": second_correlation_id,
        }
    )
    second_lease = first_execution.invocation.lease.model_copy(
        update={
            "job_id": second_job_id,
            "correlation_id": second_correlation_id,
            "lease_id": "lease-real_case_tester-job-two",
        }
    )
    second_invocation = first_execution.invocation.model_copy(
        update={
            "job_id": second_job_id,
            "correlation_id": second_correlation_id,
            "invocation_id": second_invocation_id,
            "idempotency_key": "factory-job-two-real-case-tester-attempt-1",
            "lease": second_lease,
        }
    )
    second_execution = first_execution.model_copy(
        update={
            "job_id": second_job_id,
            "correlation_id": second_correlation_id,
            "invocation_id": second_invocation_id,
            "invocation": second_invocation,
        }
    )
    return second_job, second_execution


def test_business_benchmark_cross_job_concurrent_conflict_is_normalized(
    storage: MariaDBStorage,
) -> None:
    first_job = workflow_job(mode="demo")
    first_execution = workflow_run(1)
    second_job, second_execution = _second_workflow_job_and_execution()
    setup_store = GatewayStore(storage)
    setup_store.record_factory_job(first_job)
    setup_store.record_factory_job(second_job)
    _seed_workflow_execution(setup_store, first_execution)
    _seed_workflow_execution(setup_store, second_execution)
    summaries = (
        workflow_benchmark((first_execution,)),
        workflow_benchmark((second_execution,)),
    )
    contenders = (
        GatewayStore(MariaDBStorage(TEST_DSN)),
        GatewayStore(MariaDBStorage(TEST_DSN)),
    )
    ready = Barrier(2)

    def persist(contender):
        contender_store, summary = contender
        ready.wait()
        try:
            contender_store.record_business_benchmark_summary(summary)
            return "created"
        except HTTPException as exc:
            return f"conflict:{exc.status_code}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(persist, zip(contenders, summaries, strict=True)))

    assert sorted(outcomes) == ["conflict:409", "created"]


def test_business_benchmark_event_failure_rolls_back_in_mariadb(
    storage: MariaDBStorage,
) -> None:
    factory_job = workflow_job(mode="demo")
    execution = workflow_run(1)
    summary = workflow_benchmark((execution,))
    store = GatewayStore(storage)
    store.record_factory_job(factory_job)
    _seed_workflow_execution(store, execution)
    original_insert = store._insert

    def fail_delivery_event(cursor, block):
        if block["block_type"] == "delivery_event":
            raise RuntimeError("injected delivery event failure")
        return original_insert(cursor, block)

    store._insert = fail_delivery_event  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="injected delivery event failure"):
        store.record_business_benchmark_summary(summary)

    restarted = GatewayStore(storage)
    assert restarted.business_benchmark_summary(summary.summary_id) is None
    assert restarted.delivery_events(
        project_id="agent-factory",
        run_id=str(factory_job.job_id),
    ) == ()


def test_factory_budget_routes_keep_reservations_captain_owned_and_usage_worker_owned(
    storage: MariaDBStorage,
    job_v3,
) -> None:
    reservation = InMemoryFactoryBudgetLedger().reserve(
        job_v3,
        attempt=1,
        requested_usd=Decimal("1.00"),
        now=job_v3.occurred_at,
    )
    reservation_payload = reservation.model_dump(mode="json", by_alias=True)
    with TestClient(application(storage)) as captain:
        assert captain.post(
            "/v1/factory/jobs",
            json=job_v3.model_dump(mode="json", by_alias=True),
        ).status_code == 202
    store = GatewayStore(storage)
    lease = record_usage_lease(store, job_v3)
    with TestClient(application(storage, actor=GatewayRole.WORKER)) as worker:
        assert worker.post(
            "/v1/factory/budget/reservations", json=reservation_payload
        ).status_code == 403
    with TestClient(application(storage)) as captain:
        assert captain.post(
            "/v1/factory/budget/reservations", json=reservation_payload
        ).status_code == 201
        usage = FactoryUsageReceiptV1.model_validate(usage_payload(reservation))
        usage_submission = FactoryUsageSubmissionV2(
            subject_version=job_v3.subject_version,
            lease_id=lease.lease_id,
            receipt=usage,
        )
        assert captain.post(
            "/v1/factory/budget/usage",
            json=usage_submission.model_dump(mode="json", by_alias=True),
        ).status_code == 403
    with TestClient(application(storage, actor=GatewayRole.WORKER)) as worker:
        assert worker.post(
            "/v1/factory/budget/usage",
            json=usage_submission.model_dump(mode="json", by_alias=True),
        ).status_code == 201


def test_factory_gateway_rejects_invalid_phase_before_ledger_write(storage: MariaDBStorage) -> None:
    factory_job = job()
    invalid = block(FactoryPhase.BUILD_PASSED)
    with TestClient(application(storage)) as captain:
        assert captain.post("/v1/factory/jobs", json=factory_job.model_dump(mode="json", by_alias=True)).status_code == 202
    with TestClient(application(storage, actor=GatewayRole.WORKER)) as hermes:
        response = hermes.post("/v1/factory/blocks", json=invalid.model_dump(mode="json", by_alias=True))
    with TestClient(application(storage)) as captain:
        recovered = captain.get(f"/v1/factory/jobs/{factory_job.job_id}")

    assert response.status_code == 409
    assert "illegal phase" in response.json()["detail"]
    assert recovered.json()["blocks"] == []


def test_factory_gateway_rejects_conflicting_event_replay(storage: MariaDBStorage) -> None:
    factory_job = job()
    forge = block(FactoryPhase.FORGE_REQUESTED)
    conflict = forge.model_copy(update={"occurred_at": datetime(2026, 7, 19, 11, tzinfo=timezone.utc)})
    with TestClient(application(storage)) as client:
        assert client.post("/v1/factory/jobs", json=factory_job.model_dump(mode="json", by_alias=True)).status_code == 202
        assert client.post("/v1/factory/blocks", json=forge.model_dump(mode="json", by_alias=True)).status_code == 201
        response = client.post("/v1/factory/blocks", json=conflict.model_dump(mode="json", by_alias=True))

    assert response.status_code == 409
    assert "different content" in response.json()["detail"]


def test_factory_gateway_records_only_the_next_role_lease(storage: MariaDBStorage) -> None:
    factory_job = job()
    forge = block(FactoryPhase.FORGE_REQUESTED)
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )
    renewed = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/support-triage/epoch-renewed",
        now=datetime(2026, 7, 19, 10, 5, tzinfo=timezone.utc),
    )
    with TestClient(application(storage)) as client:
        assert client.post("/v1/factory/jobs", json=factory_job.model_dump(mode="json", by_alias=True)).status_code == 202
        assert client.post("/v1/factory/blocks", json=forge.model_dump(mode="json", by_alias=True)).status_code == 201
        first = client.post("/v1/factory/leases", json=lease.model_dump(mode="json", by_alias=True))
        replay = client.post("/v1/factory/leases", json=lease.model_dump(mode="json", by_alias=True))
        renewal = client.post(
            "/v1/factory/leases",
            json=renewed.model_dump(mode="json", by_alias=True),
        )
        projection = client.get(f"/v1/factory/jobs/{factory_job.job_id}")

    assert first.status_code == 201
    assert replay.status_code == 200
    assert renewal.status_code == 201
    assert len(projection.json()["leases"]) == 2


def test_factory_gateway_allows_tool_integrator_lease_for_build_validation(storage: MariaDBStorage) -> None:
    """The same constrained tool role may create code and validate its build."""

    factory_job = job()
    architect_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/support-triage/architecture",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )
    tool_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage/tooling",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )
    build_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage/build",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )
    forge = block(FactoryPhase.FORGE_REQUESTED)
    blueprint = block(FactoryPhase.BLUEPRINT_CREATED).model_copy(
        update={"lease_id": architect_lease.lease_id}
    )
    tool_candidate = block(FactoryPhase.TOOL_CANDIDATE_TESTED).model_copy(
        update={"lease_id": tool_lease.lease_id}
    )
    agent_code = block(FactoryPhase.AGENT_CODE_CREATED).model_copy(
        update={"lease_id": tool_lease.lease_id}
    )
    with TestClient(application(storage)) as captain:
        assert captain.post("/v1/factory/jobs", json=factory_job.model_dump(mode="json", by_alias=True)).status_code == 202
        assert captain.post("/v1/factory/blocks", json=forge.model_dump(mode="json", by_alias=True)).status_code == 201
        assert captain.post("/v1/factory/leases", json=architect_lease.model_dump(mode="json", by_alias=True)).status_code == 201
    with TestClient(application(storage, actor=GatewayRole.WORKER)) as hermes:
        assert hermes.post("/v1/factory/blocks", json=blueprint.model_dump(mode="json", by_alias=True)).status_code == 201
    with TestClient(application(storage)) as captain:
        assert captain.post("/v1/factory/leases", json=tool_lease.model_dump(mode="json", by_alias=True)).status_code == 201
    with TestClient(application(storage, actor=GatewayRole.WORKER)) as hermes:
        assert hermes.post("/v1/factory/blocks", json=tool_candidate.model_dump(mode="json", by_alias=True)).status_code == 201
        assert hermes.post("/v1/factory/blocks", json=agent_code.model_dump(mode="json", by_alias=True)).status_code == 201
    with TestClient(application(storage)) as captain:
        response = captain.post("/v1/factory/leases", json=build_lease.model_dump(mode="json", by_alias=True))

    assert response.status_code == 201


def _artifact(name: str, digest: str = "a" * 64) -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://factory-gateway/{name}",
        sha256=digest,
        media_type="application/json",
    )


def _canonical_artifact(model, name: str) -> ArtifactRef:
    content = json.dumps(
        model.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _artifact(name, hashlib.sha256(content).hexdigest())


def _evaluation_submission(
    factory_job: AgentFactoryJob,
    lease,
) -> FactorySkillEvaluationSubmission:
    base = HermesSkillEvaluationEvidence.model_validate(evidence_payload(tool_gaps=[]))
    released = base.request.released_skill.model_copy(
        update={
            "released_at": lease.issued_at - timedelta(minutes=1),
            "capability": factory_job.required_capability,
        }
    )
    request = base.request.model_copy(
        update={
            "job_id": factory_job.job_id,
            "correlation_id": factory_job.correlation_id,
            "subject_id": factory_job.required_capability,
            "subject_version": factory_job.subject_version,
            "occurred_at": lease.issued_at,
            "lease": lease,
            "released_skill": released,
            "candidate_source_ref": factory_job.input_ref,
            "acceptance_assertion_ids": factory_job.acceptance_assertion_ids,
        }
    )
    receipt = base.receipt.model_copy(
        update={
            "job_id": factory_job.job_id,
            "correlation_id": factory_job.correlation_id,
            "lease_id": lease.lease_id,
            "occurred_at": lease.issued_at + timedelta(minutes=1),
            "released_skill": released,
            "used_skill_id": released.skill_id,
            "used_skill_version": released.version,
            "used_skill_sha256": released.content_sha256,
            "assertion_ids": factory_job.acceptance_assertion_ids,
        }
    )
    candidate = base.candidate.model_copy(
        update={
            "created_at": lease.issued_at + timedelta(minutes=2),
            "parent_released_skill": released,
        }
    )
    checks = tuple(
        check.model_copy(update={"occurred_at": lease.issued_at + timedelta(minutes=2 + index)})
        for index, check in enumerate(base.checks)
    )
    evidence = HermesSkillEvaluationEvidence.model_validate(
        base.model_copy(
            update={
                "job_id": factory_job.job_id,
                "correlation_id": factory_job.correlation_id,
                "subject_id": factory_job.required_capability,
                "subject_version": factory_job.subject_version,
                "occurred_at": lease.issued_at + timedelta(minutes=5),
                "request": request,
                "receipt": receipt,
                "candidate": candidate,
                "checks": checks,
                "assertion_ids": factory_job.acceptance_assertion_ids,
            }
        ).model_dump(mode="json", by_alias=True)
    )
    assert evidence.candidate is not None
    return FactorySkillEvaluationSubmission(
        evidence=evidence,
        evidence_ref=_canonical_artifact(evidence, "accepted-evaluation"),
        receipt_ref=_canonical_artifact(evidence.receipt, "usage-receipt"),
        candidate_ref=evidence.candidate.content_ref,
        tool_gap_refs=(),
    )


def _register_through_tool_lease(storage: MariaDBStorage) -> tuple[AgentFactoryJob, object]:
    factory_job = job()
    forge = block(FactoryPhase.FORGE_REQUESTED)
    architect_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/support-triage/architecture",
        now=factory_job.occurred_at,
    )
    tool_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage/tooling",
        now=factory_job.occurred_at,
    )
    blueprint = block(FactoryPhase.BLUEPRINT_CREATED).model_copy(
        update={"lease_id": architect_lease.lease_id}
    )
    with TestClient(application(storage)) as captain:
        assert captain.post("/v1/factory/jobs", json=factory_job.model_dump(mode="json", by_alias=True)).status_code == 202
        assert captain.post("/v1/factory/blocks", json=forge.model_dump(mode="json", by_alias=True)).status_code == 201
        assert captain.post("/v1/factory/leases", json=architect_lease.model_dump(mode="json", by_alias=True)).status_code == 201
    with TestClient(application(storage, actor=GatewayRole.WORKER)) as hermes:
        assert hermes.post("/v1/factory/blocks", json=blueprint.model_dump(mode="json", by_alias=True)).status_code == 201
    with TestClient(application(storage)) as captain:
        assert captain.post("/v1/factory/leases", json=tool_lease.model_dump(mode="json", by_alias=True)).status_code == 201
    return factory_job, tool_lease


def _release_submission(
    factory_job: AgentFactoryJob,
    submission: FactorySkillEvaluationSubmission,
    *,
    normal_successes: int = 3,
) -> FactoryReleaseDecisionSubmission:
    e2e = (
        E2ERunEvidence(
            run_number=1,
            correlation_id=factory_job.correlation_id,
            kind=E2EKind.RECOVERY,
            outcome=E2EOutcome.EXPECTED_FAILURE,
            evidence_ref=_artifact("recovery-e2e"),
        ),
        *(
            E2ERunEvidence(
                run_number=number,
                correlation_id=factory_job.correlation_id,
                kind=E2EKind.NORMAL,
                outcome=E2EOutcome.SUCCEEDED,
                evidence_ref=_artifact(f"normal-e2e-{number}"),
            )
            for number in range(2, normal_successes + 2)
        ),
    )
    evaluation = GatewayStore._stored_factory_evaluation(submission)
    return FactoryReleaseDecisionSubmission(
        decision=evaluate_factory_release(factory_job, e2e, evaluation),
        e2e_evidence=e2e,
    )


def test_factory_evaluation_is_lease_bound_idempotent_and_reference_checked(
    storage: MariaDBStorage,
) -> None:
    factory_job, tool_lease = _register_through_tool_lease(storage)
    with TestClient(application(storage)) as captain:
        submission = _evaluation_submission(factory_job, tool_lease)
        skill = submission.evidence.request.released_skill
        assert captain.post(
            "/v1/factory/skills/releases",
            json=skill.model_dump(mode="json", by_alias=True),
        ).status_code == 201

    with TestClient(application(storage, actor=GatewayRole.WORKER)) as hermes:
        bad_evidence_ref = submission.model_copy(
            update={
                "evidence_ref": submission.evidence_ref.model_copy(
                    update={"sha256": "0" * 64}
                )
            }
        )
        bad_receipt_ref = submission.model_copy(
            update={
                "receipt_ref": submission.receipt_ref.model_copy(
                    update={"sha256": "1" * 64}
                )
            }
        )
        evidence_ref_rejected = hermes.post(
            "/v1/factory/evaluations",
            json=bad_evidence_ref.model_dump(mode="json", by_alias=True),
        )
        receipt_ref_rejected = hermes.post(
            "/v1/factory/evaluations",
            json=bad_receipt_ref.model_dump(mode="json", by_alias=True),
        )
        first = hermes.post(
            "/v1/factory/evaluations",
            json=submission.model_dump(mode="json", by_alias=True),
        )
        replay = hermes.post(
            "/v1/factory/evaluations",
            json=submission.model_dump(mode="json", by_alias=True),
        )
        changed = submission.model_copy(update={"evidence_ref": _artifact("changed")})
        conflict = hermes.post(
            "/v1/factory/evaluations",
            json=changed.model_dump(mode="json", by_alias=True),
        )
        unknown = submission.model_copy(update={"candidate_ref": _artifact("unknown-candidate")})
        unknown_response = hermes.post(
            "/v1/factory/evaluations",
            json=unknown.model_dump(mode="json", by_alias=True),
        )

    assert evidence_ref_rejected.status_code == 409
    assert receipt_ref_rejected.status_code == 409
    assert first.status_code == 201
    assert first.json()["replayed"] is False
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert conflict.status_code == 409
    assert unknown_response.status_code == 409


def test_factory_evaluation_rejects_missing_expired_and_cross_job_lease(
    storage: MariaDBStorage,
) -> None:
    factory_job = job()
    unrecorded_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage/unrecorded",
        now=factory_job.occurred_at,
    )
    missing = _evaluation_submission(factory_job, unrecorded_lease)
    with TestClient(application(storage, actor=GatewayRole.WORKER)) as hermes:
        missing_response = hermes.post(
            "/v1/factory/evaluations",
            json=missing.model_dump(mode="json", by_alias=True),
        )
    assert missing_response.status_code == 409

    factory_job, tool_lease = _register_through_tool_lease(storage)
    with TestClient(application(storage)) as captain:
        valid = _evaluation_submission(factory_job, tool_lease)
        assert captain.post(
            "/v1/factory/skills/releases",
            json=valid.evidence.request.released_skill.model_dump(mode="json", by_alias=True),
        ).status_code == 201

    expired = valid.model_dump(mode="json", by_alias=True)
    expired["evidence"]["occurred_at"] = tool_lease.expires_at.isoformat()
    cross_job = valid.model_dump(mode="json", by_alias=True)
    cross_job["evidence"]["job_id"] = str(UUID("00000000-0000-0000-0000-000000000999"))
    with TestClient(application(storage, actor=GatewayRole.WORKER)) as hermes:
        expired_response = hermes.post("/v1/factory/evaluations", json=expired)
        cross_job_response = hermes.post("/v1/factory/evaluations", json=cross_job)
    assert expired_response.status_code in {409, 422}
    assert cross_job_response.status_code in {409, 422}


def test_captain_publication_then_promotion_is_authoritative(
    storage: MariaDBStorage,
) -> None:
    factory_job, tool_lease = _register_through_tool_lease(storage)
    with TestClient(application(storage)) as captain:
        submission = _evaluation_submission(factory_job, tool_lease)
        assert captain.post(
            "/v1/factory/skills/releases",
            json=submission.evidence.request.released_skill.model_dump(mode="json", by_alias=True),
        ).status_code == 201
    with TestClient(application(storage, actor=GatewayRole.WORKER)) as hermes:
        assert hermes.post(
            "/v1/factory/evaluations",
            json=submission.model_dump(mode="json", by_alias=True),
        ).status_code == 201

        tool_candidate = block(FactoryPhase.TOOL_CANDIDATE_TESTED).model_copy(
            update={"lease_id": tool_lease.lease_id}
        )
        agent_code = block(FactoryPhase.AGENT_CODE_CREATED).model_copy(
            update={"lease_id": tool_lease.lease_id}
        )
        build = block(FactoryPhase.BUILD_PASSED).model_copy(
            update={"lease_id": tool_lease.lease_id}
        )
        for lifecycle_block in (tool_candidate, agent_code, build):
            assert hermes.post(
                "/v1/factory/blocks",
                json=lifecycle_block.model_dump(mode="json", by_alias=True),
            ).status_code == 201

    real_case_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        workspace_ref="workspace://factory/support-triage/real-case",
        now=factory_job.occurred_at,
    )
    with TestClient(application(storage)) as captain:
        assert captain.post(
            "/v1/factory/leases",
            json=real_case_lease.model_dump(mode="json", by_alias=True),
        ).status_code == 201
    real_case = block(
        FactoryPhase.REAL_CASE_EVIDENCE,
        assertions=("real_case_green",),
    ).model_copy(update={"lease_id": real_case_lease.lease_id})
    with TestClient(application(storage, actor=GatewayRole.WORKER)) as hermes:
        assert hermes.post(
            "/v1/factory/blocks",
            json=real_case.model_dump(mode="json", by_alias=True),
        ).status_code == 201

    quality_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.QUALITY_WARDEN,
        attempt=1,
        workspace_ref="workspace://factory/support-triage/quality",
        now=factory_job.occurred_at,
    )
    with TestClient(application(storage)) as captain:
        assert captain.post(
            "/v1/factory/leases",
            json=quality_lease.model_dump(mode="json", by_alias=True),
        ).status_code == 201
    quality = block(
        FactoryPhase.QUALITY_REVIEWED,
        assertions=("schema_valid",),
    ).model_copy(update={"lease_id": quality_lease.lease_id})
    with TestClient(application(storage, actor=GatewayRole.WORKER)) as hermes:
        assert hermes.post(
            "/v1/factory/blocks",
            json=quality.model_dump(mode="json", by_alias=True),
        ).status_code == 201

    assert submission.evidence.candidate is not None
    publication = PublishedHermesSkill(
        skill_id=submission.evidence.request.released_skill.skill_id,
        version=submission.evidence.request.released_skill.version + 1,
        candidate_id=submission.evidence.candidate.candidate_id,
        evaluation_id=submission.evidence.evidence_id,
        content_ref=submission.evidence.candidate.content_ref,
        content_sha256=submission.evidence.candidate.content_sha256,
        published_at=submission.evidence.occurred_at + timedelta(minutes=1),
        producer="captain",
        status="published",
    )
    promotion = block(
        FactoryPhase.CAPABILITY_PROMOTED,
        assertions=factory_job.acceptance_assertion_ids,
    ).model_copy(
        update={
            "occurred_at": publication.published_at + timedelta(minutes=1),
            "evidence_refs": (submission.evidence_ref,),
        }
    )
    release_submission = _release_submission(factory_job, submission)
    blocked_release_submission = _release_submission(
        factory_job,
        submission,
        normal_successes=2,
    )

    with TestClient(application(storage, actor=GatewayRole.WORKER)) as hermes:
        assert hermes.post(
            "/v1/factory/release-decisions",
            json=release_submission.model_dump(mode="json", by_alias=True),
        ).status_code == 403
        assert hermes.post(
            "/v1/factory/skills/publications",
            json=publication.model_dump(mode="json", by_alias=True),
        ).status_code == 403
        assert hermes.post(
            "/v1/factory/blocks",
            json=promotion.model_dump(mode="json", by_alias=True),
        ).status_code == 403

    with TestClient(application(storage)) as captain:
        decision = captain.post(
            "/v1/factory/release-decisions",
            json=release_submission.model_dump(mode="json", by_alias=True),
        )
        published = captain.post(
            "/v1/factory/skills/publications",
            json=publication.model_dump(mode="json", by_alias=True),
        )
        promoted = captain.post(
            "/v1/factory/blocks",
            json=promotion.model_dump(mode="json", by_alias=True),
        )
        decision_replay = captain.post(
            "/v1/factory/release-decisions",
            json=release_submission.model_dump(mode="json", by_alias=True),
        )
        late_decision = captain.post(
            "/v1/factory/release-decisions",
            json=blocked_release_submission.model_dump(mode="json", by_alias=True),
        )
        projection = captain.get(f"/v1/factory/jobs/{factory_job.job_id}")
        evaluation = captain.get(f"/v1/factory/evaluations/{factory_job.job_id}")

    assert decision.status_code == 201
    assert published.status_code == 201
    assert promoted.status_code == 201
    assert decision_replay.status_code == 200
    assert decision_replay.json()["replayed"] is True
    assert late_decision.status_code == 409
    assert late_decision.json()["detail"] == (
        "Factory release decisions are sealed after capability promotion"
    )
    assert projection.json()["projection"]["status"] == "ready_to_use"
    assert evaluation.json()["evidence"]["evidence_id"] == str(
        submission.evidence.evidence_id
    )

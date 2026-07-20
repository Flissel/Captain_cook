from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agenten.agent_factory.contracts import AgentFactoryJob, FactoryPhase, FactoryRole
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.skill_evaluation import HermesSkillEvaluationEvidence
from agenten.agent_runtime.contracts import ArtifactRef
from blockchain.mariadb_storage import MariaDBStorage
from gateway.app import create_app
from gateway.auth import GatewayRole, require_actor
from gateway.contracts import (
    FactorySkillEvaluationSubmission,
    PublishedHermesSkill,
)
from gateway.settings import GatewaySettings
from tests.agent_factory.test_state_machine import block, job
from tests.agent_factory.test_skill_evaluation_contracts import evidence_payload
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
    with TestClient(application(storage)) as client:
        assert client.post("/v1/factory/jobs", json=factory_job.model_dump(mode="json", by_alias=True)).status_code == 202
        assert client.post("/v1/factory/blocks", json=forge.model_dump(mode="json", by_alias=True)).status_code == 201
        first = client.post("/v1/factory/leases", json=lease.model_dump(mode="json", by_alias=True))
        replay = client.post("/v1/factory/leases", json=lease.model_dump(mode="json", by_alias=True))
        projection = client.get(f"/v1/factory/jobs/{factory_job.job_id}")

    assert first.status_code == 201
    assert replay.status_code == 200
    assert len(projection.json()["leases"]) == 1


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

    with TestClient(application(storage, actor=GatewayRole.WORKER)) as hermes:
        assert hermes.post(
            "/v1/factory/skills/publications",
            json=publication.model_dump(mode="json", by_alias=True),
        ).status_code == 403
        assert hermes.post(
            "/v1/factory/blocks",
            json=promotion.model_dump(mode="json", by_alias=True),
        ).status_code == 403

    with TestClient(application(storage)) as captain:
        published = captain.post(
            "/v1/factory/skills/publications",
            json=publication.model_dump(mode="json", by_alias=True),
        )
        promoted = captain.post(
            "/v1/factory/blocks",
            json=promotion.model_dump(mode="json", by_alias=True),
        )
        projection = captain.get(f"/v1/factory/jobs/{factory_job.job_id}")
        evaluation = captain.get(f"/v1/factory/evaluations/{factory_job.job_id}")

    assert published.status_code == 201
    assert promoted.status_code == 201
    assert projection.json()["projection"]["status"] == "ready_to_use"
    assert evaluation.json()["evidence"]["evidence_id"] == str(
        submission.evidence.evidence_id
    )

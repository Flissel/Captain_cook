from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agenten.agent_factory.contracts import (
    FactoryJob,
    FactoryPhase,
    FactoryRole,
    PromotedCapability,
)
from agenten.agent_factory.outcome_contracts import ExecutionOutcomeV1
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.release_gate import (
    E2EKind,
    E2EOutcome,
    E2ERunEvidence,
    evaluate_factory_release,
    derive_terminal_decision,
)
from agenten.agent_factory.state_machine import FactoryProjection
from agenten.agent_factory.skill_evaluation import HermesSkillEvaluationEvidence
from agenten.agent_runtime.contracts import ArtifactRef
from blockchain.mariadb_storage import MariaDBStorage
from gateway.app import create_app
from gateway.auth import GatewayRole, require_actor
from gateway.contracts import (
    CapabilityExecutionRequest,
    CapabilityReleaseRequest,
    FactorySkillEvaluationSubmission,
    FactoryReleaseDecisionSubmission,
    PublishedHermesSkill,
    RuntimeExecutionClaimRequest,
)
from gateway.settings import GatewaySettings
from gateway.store import GatewayStore
from tests.agent_factory.test_release_gate import accepted_manifest, capability_e2e
from tests.agent_factory.test_state_machine import NOW, block, job, v2_job
from tests.agent_factory.test_skill_evaluation_contracts import evidence_payload
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")
FIXTURES = Path(__file__).parents[1] / "fixtures" / "contracts"


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
    factory_job: FactoryJob,
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


def _register_through_tool_lease(
    storage: MariaDBStorage,
    factory_job: FactoryJob | None = None,
) -> tuple[FactoryJob, object]:
    factory_job = factory_job or job()
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
    factory_job: FactoryJob,
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


def _prepare_v2_capability_release(
    storage: MariaDBStorage,
    *,
    promote: bool,
) -> CapabilityReleaseRequest:
    factory_job, tool_lease = _register_through_tool_lease(storage, v2_job())
    submission = _evaluation_submission(factory_job, tool_lease)
    with TestClient(application(storage)) as captain:
        released = captain.post(
            "/v1/factory/skills/releases",
            json=submission.evidence.request.released_skill.model_dump(
                mode="json", by_alias=True
            ),
        )
    assert released.status_code == 201, released.text
    with TestClient(application(storage, actor=GatewayRole.WORKER)) as hermes:
        evaluated = hermes.post(
            "/v1/factory/evaluations",
            json=submission.model_dump(mode="json", by_alias=True),
        )
        assert evaluated.status_code == 201, evaluated.text
        for phase in (
            FactoryPhase.TOOL_CANDIDATE_TESTED,
            FactoryPhase.AGENT_CODE_CREATED,
            FactoryPhase.BUILD_PASSED,
        ):
            evidence = block(phase).model_copy(
                update={"lease_id": tool_lease.lease_id}
            )
            recorded = hermes.post(
                "/v1/factory/blocks",
                json=evidence.model_dump(mode="json", by_alias=True),
            )
            assert recorded.status_code == 201, recorded.text

    real_case_lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        workspace_ref="workspace://factory/support-triage/real-case",
        now=factory_job.occurred_at,
    )
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
            json=real_case_lease.model_dump(mode="json", by_alias=True),
        ).status_code == 201
    real_case = block(
        FactoryPhase.REAL_CASE_EVIDENCE,
        assertions=("real_case_green",),
    ).model_copy(update={"lease_id": real_case_lease.lease_id})
    with TestClient(application(storage, actor=GatewayRole.WORKER)) as hermes:
        recorded = hermes.post(
            "/v1/factory/blocks",
            json=real_case.model_dump(mode="json", by_alias=True),
        )
        assert recorded.status_code == 201, recorded.text
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
        recorded = hermes.post(
            "/v1/factory/blocks",
            json=quality.model_dump(mode="json", by_alias=True),
        )
        assert recorded.status_code == 201, recorded.text

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
    with TestClient(application(storage)) as captain:
        decision_response = captain.post(
            "/v1/factory/release-decisions",
            json=release_submission.model_dump(mode="json", by_alias=True),
        )
        publication_response = captain.post(
            "/v1/factory/skills/publications",
            json=publication.model_dump(mode="json", by_alias=True),
        )
        assert decision_response.status_code == 201, decision_response.text
        assert publication_response.status_code == 201, publication_response.text
        if promote:
            promotion_response = captain.post(
                "/v1/factory/blocks",
                json=promotion.model_dump(mode="json", by_alias=True),
            )
            assert promotion_response.status_code == 201, promotion_response.text

    release_evidence = capability_e2e()
    package = accepted_manifest(e2e=release_evidence)
    evaluation = GatewayStore._stored_factory_evaluation(submission)
    decided_at = NOW + timedelta(minutes=10)
    terminal = derive_terminal_decision(
        factory_job,
        FactoryProjection.from_job(factory_job),
        package,
        evaluation,
        release_evidence,
        decided_at,
    )
    assert terminal is not None
    autogen_ref = next(
        artifact.reference
        for artifact in package.artifacts
        if artifact.kind == "autogen_source"
    )
    intents = tuple(
        sorted(
            {
                outcome.integration_intent
                for outcome in package.assertion_outcomes
                if outcome.integration_intent.value != "none"
            },
            key=lambda item: item.value,
        )
    )
    return CapabilityReleaseRequest(
        schema_name="captain.capability-release-request.v1",
        event_id=UUID("00000000-0000-0000-0000-000000000710"),
        causation_id=factory_job.event_id,
        occurred_at=decided_at,
        producer="captain",
        decision=terminal,
        decision_ref=_canonical_artifact(terminal, "terminal-decision"),
        package=package,
        package_ref=_canonical_artifact(package, "capability-package"),
        release_evidence=release_evidence,
        promoted_capability=PromotedCapability(
            capability_id=package.capability_id,
            version=package.capability_version,
            status="ready_to_use",
            blueprint_ref=package.team_manifest_ref,
            code_ref=autogen_ref,
            promotion_block_ref=_canonical_artifact(promotion, "promotion"),
        ),
        schema_major=1,
        team_version=1,
        accepted_assertion_ids=tuple(
            outcome.assertion_id for outcome in package.assertion_outcomes
        ),
        integration_intents=intents,
        tool_contracts=(),
    )


def _table_counts(storage: MariaDBStorage) -> tuple[int, int, int]:
    with storage.transaction() as connection:
        with connection.cursor() as cursor:
            counts = []
            for table in (
                "factory_terminal_decisions",
                "factory_capability_packages",
                "factory_capability_catalog",
            ):
                cursor.execute(f"SELECT COUNT(*) AS count FROM {table}")
                counts.append(int(cursor.fetchone()["count"]))
    return tuple(counts)  # type: ignore[return-value]


def test_capability_release_crash_rolls_back_then_retry_exposes_exactly_one(
    storage: MariaDBStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _prepare_v2_capability_release(storage, promote=True)
    store = GatewayStore(storage)
    insert = store._insert

    def crash_after_package(cursor, ledger_block) -> None:
        insert(cursor, ledger_block)
        if ledger_block.block_type == "capability_package_published":
            raise RuntimeError("injected release crash")

    monkeypatch.setattr(store, "_insert", crash_after_package)
    with pytest.raises(RuntimeError, match="injected release crash"):
        store.publish_capability_release(request)

    assert _table_counts(storage) == (0, 0, 0)
    assert store.factory_terminal_decision(request.package.factory_job_id) is None
    assert store.capability(request.package.capability_id) is None

    monkeypatch.setattr(store, "_insert", insert)
    first = store.publish_capability_release(request)
    replay = GatewayStore(storage).publish_capability_release(request)

    assert first.replayed is False
    assert replay.replayed is True
    assert _table_counts(storage) == (1, 1, 1)
    assert store.capability(request.package.capability_id) is not None
    with pytest.raises(HTTPException, match="different content"):
        store.publish_capability_release(request.model_copy(update={"team_version": 2}))


def test_capability_release_rejects_pre_promotion_and_worker_authority(
    storage: MariaDBStorage,
) -> None:
    request = _prepare_v2_capability_release(storage, promote=False)
    body = request.model_dump(mode="json", by_alias=True)
    with TestClient(application(storage, actor=GatewayRole.WORKER)) as worker:
        assert worker.post("/v1/capabilities", json=body).status_code == 403
    with TestClient(application(storage)) as captain:
        response = captain.post("/v1/capabilities", json=body)
    assert response.status_code == 409
    assert "promotion" in response.json()["detail"]
    assert _table_counts(storage) == (0, 0, 0)


def _load_contract(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _seed_runtime_result(storage: MariaDBStorage) -> tuple[dict[str, object], dict[str, object]]:
    command = _load_contract("agent_runtime_command.v1.json")
    result = _load_contract("agent_runtime_result.v1.json")
    issued_at = datetime(2026, 7, 21, 10, 1, tzinfo=timezone.utc)
    command["occurred_at"] = datetime(2026, 7, 21, 10, tzinfo=timezone.utc).isoformat()
    result["occurred_at"] = datetime(2026, 7, 21, 10, 2, tzinfo=timezone.utc).isoformat()
    grant = {
        "schema": "captain.capability-grant.v1",
        "grant_id": result["grant_id"],
        "command_id": command["event_id"],
        "batch_id": "batch-1",
        "batch_version": command["subject_version"],
        "subtask_id": "subtask-1",
        "workspace_ref": "workspace://authorized/project-1/subtask-1",
        "profile": "n8n-builder",
        "capabilities": [
            "codex.cancel",
            "codex.heartbeat",
            "codex.resume",
            "codex.run",
            "codex.status",
            "mcp.n8n",
            "tests.run",
            "workspace.write",
        ],
        "mcp_servers": ["n8n-mcp"],
        "issued_at": issued_at.isoformat(),
        "expires_at": (issued_at + timedelta(minutes=15)).isoformat(),
    }
    batch = {
        "block_type": "work_batch",
        "status": "pending",
        "data": {
            "batch_id": "batch-1",
            "title": "Runtime integration",
            "goal": "Execute the released runtime command",
            "subtask_ids": ["subtask-1"],
            "target": "python",
            "capability_tags": ["n8n-builder"],
            "acceptance_criteria": [
                {
                    "assertion_id": "runtime-result",
                    "kind": "status_equals",
                    "expected": "succeeded",
                }
            ],
        },
    }
    with TestClient(application(storage)) as captain:
        assert captain.post("/blocks", json=batch).status_code == 201
        assert captain.post("/v1/runtime/commands", json=command).status_code == 202
        assert captain.post("/v1/runtime/grants", json=grant).status_code == 201
        claim = captain.post(
            "/v1/runtime/execution-claims",
            json={
                "schema": "captain.runtime-execution-claim-request.v1",
                "command_id": command["event_id"],
                "owner_id": "runtime-a",
                "lease_seconds": 60,
            },
        )
        assert claim.status_code == 201, claim.text
        fence = claim.json()["claim"]["fencing_token"]
        recorded = captain.post(
            "/v1/runtime/results",
            json=result,
            headers={
                "X-Runtime-Owner-ID": "runtime-a",
                "X-Runtime-Fencing-Token": str(fence),
            },
        )
        assert recorded.status_code == 201, recorded.text
    return command, result


def _execution_request(
    release: CapabilityReleaseRequest,
    command: dict[str, object],
    result: dict[str, object],
) -> CapabilityExecutionRequest:
    payload = _load_contract("execution_outcome.v1.json")
    payload.update(
        {
            "capability_id": release.package.capability_id,
            "capability_version": release.package.capability_version,
            "team_version": release.team_version,
            "correlation_id": command["correlation_id"],
            "command_id": command["event_id"],
            "result_id": result["event_id"],
            "assertion_outcomes": [
                item.model_dump(mode="json")
                for item in release.package.assertion_outcomes
            ],
        }
    )
    outcome = ExecutionOutcomeV1.model_validate(payload)
    return CapabilityExecutionRequest(
        schema_name="captain.capability-execution-request.v1",
        event_id=UUID("00000000-0000-0000-0000-000000000711"),
        causation_id=outcome.result_id,
        occurred_at=datetime.fromisoformat(str(result["occurred_at"])),
        producer="captain",
        outcome=outcome,
        outcome_ref=_canonical_artifact(outcome, "execution-outcome"),
        claim_owner_id="runtime-a",
        claim_fencing_token=1,
    )


def test_capability_execution_rejects_unpublished_and_stale_then_replays(
    storage: MariaDBStorage,
) -> None:
    release = _prepare_v2_capability_release(storage, promote=True)
    command, result = _seed_runtime_result(storage)
    execution = _execution_request(release, command, result)
    body = execution.model_dump(mode="json", by_alias=True)
    with TestClient(application(storage)) as captain:
        assert captain.post("/v1/capability-executions", json=body).status_code == 409
        assert captain.post(
            "/v1/capabilities",
            json=release.model_dump(mode="json", by_alias=True),
        ).status_code == 201
        stale_outcome = execution.outcome.model_copy(
            update={"capability_version": release.package.capability_version + 1}
        )
        stale = execution.model_copy(
            update={
                "event_id": UUID("00000000-0000-0000-0000-000000000712"),
                "outcome": stale_outcome,
                "outcome_ref": _canonical_artifact(stale_outcome, "stale-outcome"),
            }
        )
        assert captain.post(
            "/v1/capability-executions",
            json=stale.model_dump(mode="json", by_alias=True),
        ).status_code == 409
        first = captain.post("/v1/capability-executions", json=body)
        replay = captain.post("/v1/capability-executions", json=body)
        read = captain.get(f"/v1/capability-executions/{execution.outcome.command_id}")

    assert first.status_code == 201, first.text
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert read.status_code == 200
    assert read.json()["result_id"] == str(execution.outcome.result_id)


def test_concurrent_runtime_claim_has_one_owner_and_restart_safe_recovery(
    storage: MariaDBStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _load_contract("agent_runtime_command.v1.json")
    batch = {
        "block_type": "work_batch",
        "status": "pending",
        "data": {
            "batch_id": "batch-1",
            "title": "Runtime integration",
            "goal": "Execute the released runtime command",
            "subtask_ids": ["subtask-1"],
            "target": "python",
            "capability_tags": ["n8n-builder"],
            "acceptance_criteria": [
                {
                    "assertion_id": "runtime-result",
                    "kind": "status_equals",
                    "expected": "succeeded",
                }
            ],
        },
    }
    with TestClient(application(storage)) as captain:
        assert captain.post("/blocks", json=batch).status_code == 201
        assert captain.post("/v1/runtime/commands", json=command).status_code == 202

    monkeypatch.setattr("gateway.store._utcnow", lambda: NOW)
    store = GatewayStore(storage)

    def attempt(owner: str) -> tuple[str, object]:
        request = RuntimeExecutionClaimRequest(
            schema_name="captain.runtime-execution-claim-request.v1",
            command_id=UUID(str(command["event_id"])),
            owner_id=owner,
            lease_seconds=60,
        )
        try:
            return "claimed", store.claim_runtime_execution(request)
        except HTTPException as exc:
            return "conflict", exc.detail

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(attempt, ("runtime-a", "runtime-b")))

    assert [kind for kind, _ in outcomes].count("claimed") == 1
    assert [kind for kind, _ in outcomes].count("conflict") == 1
    winner = next(value.claim.owner_id for kind, value in outcomes if kind == "claimed")
    restarted = GatewayStore(storage)
    replay = restarted.claim_runtime_execution(
        RuntimeExecutionClaimRequest(
            schema_name="captain.runtime-execution-claim-request.v1",
            command_id=UUID(str(command["event_id"])),
            owner_id=winner,
            lease_seconds=60,
        )
    )
    assert replay.replayed is True

    monkeypatch.setattr("gateway.store._utcnow", lambda: NOW + timedelta(seconds=61))
    recovered = restarted.claim_runtime_execution(
        RuntimeExecutionClaimRequest(
            schema_name="captain.runtime-execution-claim-request.v1",
            command_id=UUID(str(command["event_id"])),
            owner_id="runtime-recovery",
            lease_seconds=60,
        )
    )
    assert recovered.recovered is True
    assert recovered.claim.fencing_token == 2

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import HTTPException

from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.service import FactoryCoordinator
from agenten.agent_factory.skill_evaluation import HermesSkillEvaluationEvidence
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.contracts import FactorySkillEvaluationSubmission, PublishedHermesSkill
from gateway.factory_repository import GatewayFactoryLeases, GatewayFactoryRepository
from gateway.store import GatewayStore
from tests.agent_factory.test_state_machine import accepted_evaluation, block, job


class Store:
    def __init__(self) -> None:
        self.jobs = {}
        self.events = {}
        self.evaluations = {}

    def record_factory_job(self, factory_job):
        self.jobs.setdefault(factory_job.job_id, factory_job)
        return type("Receipt", (), {"replayed": False})()

    def record_factory_block(self, evidence):
        self.events.setdefault(evidence.job_id, []).append(evidence)
        return type("Receipt", (), {"replayed": False})()

    def factory_job(self, job_id):
        return type("Projection", (), {"job": self.jobs[job_id], "blocks": tuple(self.events.get(job_id, ()))})()

    def factory_skill_evaluation(self, job_id):
        return self.evaluations.get(job_id)


def test_gateway_adapter_runs_coordinator_against_gateway_store_shape() -> None:
    coordinator = FactoryCoordinator(GatewayFactoryRepository(Store()))
    factory_job = job()

    coordinator.register(factory_job)
    coordinator.record(block(FactoryPhase.FORGE_REQUESTED))

    assert coordinator.projection(factory_job.job_id).phase is FactoryPhase.FORGE_REQUESTED


def test_gateway_leases_resolve_only_the_current_role_attempt() -> None:
    store = Store()
    factory_job = job()
    store.jobs[factory_job.job_id] = factory_job
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=factory_job.occurred_at,
    )
    store.factory_leases = (lease,)
    store.factory_job = lambda _job_id: type(
        "Projection", (), {"job": factory_job, "blocks": (), "leases": store.factory_leases}
    )()

    resolved = GatewayFactoryLeases(store).active(
        factory_job, FactoryRole.AGENT_ARCHITECT, 1, factory_job.occurred_at
    )

    assert resolved.lease_id == lease.lease_id


def test_gateway_adapter_reads_the_gateway_owned_skill_evaluation() -> None:
    store = Store()
    factory_job = job()
    evaluation = accepted_evaluation()
    store.jobs[factory_job.job_id] = factory_job
    store.evaluations[factory_job.job_id] = evaluation

    assert GatewayFactoryRepository(store).evaluation_for_job(factory_job.job_id) == evaluation


def _canonical_ref(model, name: str) -> ArtifactRef:
    content = json.dumps(
        model.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ArtifactRef(
        uri=f"artifact://factory-gateway/{name}",
        sha256=hashlib.sha256(content).hexdigest(),
        media_type="application/json",
    )


def _submission() -> FactorySkillEvaluationSubmission:
    factory_job = job()
    base = accepted_evaluation().evidence
    released = base.request.released_skill.model_copy(
        update={"capability": factory_job.required_capability}
    )
    request = base.request.model_copy(
        update={
            "subject_id": factory_job.required_capability,
            "candidate_source_ref": factory_job.input_ref,
            "released_skill": released,
        }
    )
    receipt = base.receipt.model_copy(
        update={
            "released_skill": released,
            "used_skill_id": released.skill_id,
            "used_skill_version": released.version,
            "used_skill_sha256": released.content_sha256,
        }
    )
    assert base.candidate is not None
    candidate = base.candidate.model_copy(update={"parent_released_skill": released})
    evidence = HermesSkillEvaluationEvidence.model_validate(
        base.model_copy(
            update={
                "subject_id": factory_job.required_capability,
                "request": request,
                "receipt": receipt,
                "candidate": candidate,
                "tool_gaps": (),
            }
        ).model_dump(mode="json", by_alias=True)
    )
    return FactorySkillEvaluationSubmission(
        evidence=evidence,
        evidence_ref=_canonical_ref(evidence, "evaluation"),
        receipt_ref=_canonical_ref(evidence.receipt, "receipt"),
        candidate_ref=candidate.content_ref,
        tool_gap_refs=(),
    )


def _publication(submission: FactorySkillEvaluationSubmission) -> PublishedHermesSkill:
    evidence = submission.evidence
    assert evidence.candidate is not None
    return PublishedHermesSkill(
        skill_id=evidence.request.released_skill.skill_id,
        version=evidence.request.released_skill.version + 1,
        candidate_id=evidence.candidate.candidate_id,
        evaluation_id=evidence.evidence_id,
        content_ref=evidence.candidate.content_ref,
        content_sha256=evidence.candidate.content_sha256,
        published_at=evidence.occurred_at,
        producer="captain",
        status="published",
    )


def test_gateway_rejects_skill_evaluation_for_another_capability() -> None:
    submission = _submission()
    evidence = submission.evidence
    other_skill = evidence.request.released_skill.model_copy(
        update={"capability": "other_capability"}
    )
    request = evidence.request.model_copy(update={"released_skill": other_skill})
    receipt = evidence.receipt.model_copy(update={"released_skill": other_skill})
    assert evidence.candidate is not None
    candidate = evidence.candidate.model_copy(update={"parent_released_skill": other_skill})
    mismatched = evidence.model_copy(
        update={"request": request, "receipt": receipt, "candidate": candidate}
    )

    with pytest.raises(HTTPException, match="capability"):
        GatewayStore._assert_factory_evaluation_job(mismatched, job())


@pytest.mark.parametrize(("field", "digest"), (("evidence_ref", "0" * 64), ("receipt_ref", "1" * 64)))
def test_gateway_rejects_unknown_or_digest_mismatched_aggregate_reference(
    field: str,
    digest: str,
) -> None:
    submission = _submission()
    changed = submission.model_copy(
        update={field: submission.evidence_ref.model_copy(update={"sha256": digest})}
    )

    with pytest.raises(HTTPException, match="digest"):
        GatewayStore._assert_evaluation_references(changed)


def test_gateway_publication_uses_the_full_factory_evaluation_qualification() -> None:
    submission = _submission()
    failed_checks = tuple(
        check.model_copy(update={"status": "failed"})
        if check.kind == "build"
        else check
        for check in submission.evidence.checks
    )
    failed_evidence = submission.evidence.model_copy(update={"checks": failed_checks})
    unqualified = submission.model_copy(
        update={
            "evidence": failed_evidence,
            "evidence_ref": _canonical_ref(failed_evidence, "failed-evaluation"),
        }
    )

    with pytest.raises(HTTPException, match="skill candidate evaluator did not succeed"):
        GatewayStore._assert_publication_qualification(
            _publication(unqualified),
            unqualified,
            job(),
        )

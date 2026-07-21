from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import gateway.contracts as gateway_contracts

from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.release_gate import E2EKind, E2EOutcome, E2ERunEvidence
from agenten.agent_factory.service import FactoryCoordinator, InMemoryFactoryRepository
from agenten.agent_factory.skill_evaluation import HermesSkillEvaluationEvidence
from agenten.agent_factory.skill_workflow_contracts import (
    CodebaseInventoryV1,
    FactorySkillStep,
)
from agenten.agent_factory.state_machine import FactoryLifecycleStatus, FactoryProjection
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.contracts import (
    FactorySkillEvaluationSubmission,
    PublishedHermesSkill,
    parse_factory_workflow_artifact,
)
from gateway.factory_repository import GatewayFactoryLeases, GatewayFactoryRepository
from gateway.store import GatewayStore
from tests.agent_factory.test_state_machine import (
    accepted_evaluation,
    accepted_release_decision,
    artifact,
    block,
    job,
)
from tests.agent_factory.test_execution_budget import job_v3
from tests.agent_factory.test_skill_workflow_contracts import (
    brief_payload,
    evaluation_payload,
    execution_payload,
    feedback_payload,
    inventory_payload,
    revision_payload,
)


class Store:
    def __init__(self) -> None:
        self.jobs = {}
        self.events = {}
        self.evaluations = {}
        self.decisions = {}
        self.workflow_artifacts = {}

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

    def factory_release_decision(self, job_id):
        return self.decisions.get(job_id)

    def factory_workflow_artifacts(self, job_id):
        return tuple(self.workflow_artifacts.get(job_id, ()))


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


def test_gateway_adapter_reads_the_gateway_accepted_release_decision() -> None:
    store = Store()
    factory_job = job()
    decision = accepted_release_decision()
    store.jobs[factory_job.job_id] = factory_job
    store.decisions[factory_job.job_id] = decision

    assert GatewayFactoryRepository(store).release_decision_for_job(factory_job.job_id) == decision


def test_gateway_repository_round_trips_a_v3_job_without_schema_loss(job_v3) -> None:
    store = Store()
    repository = GatewayFactoryRepository(store)

    repository.register(job_v3)

    assert repository.job(job_v3.job_id) == job_v3
    assert repository.job(job_v3.job_id).schema_name == "captain.agent-factory-job.v3"


def test_factory_repository_port_and_coordinator_preserve_v3_jobs(job_v3) -> None:
    coordinator = FactoryCoordinator(InMemoryFactoryRepository())

    coordinator.register(job_v3)

    assert coordinator.projection(job_v3.job_id).job == job_v3
    assert coordinator.projection(job_v3.job_id).job.execution_policy == (
        job_v3.execution_policy
    )


def test_gateway_repository_exposes_workflow_artifacts_read_only() -> None:
    artifact = CodebaseInventoryV1.model_validate(inventory_payload())
    store = Store()
    store.workflow_artifacts[artifact.job_id] = [artifact]

    recovered = GatewayFactoryRepository(store).workflow_artifacts(artifact.job_id)

    assert recovered == (artifact,)


@pytest.mark.parametrize(
    "payload_builder",
    (
        inventory_payload,
        brief_payload,
        execution_payload,
        evaluation_payload,
        revision_payload,
        feedback_payload,
    ),
)
def test_gateway_parser_preserves_each_workflow_artifact_schema(payload_builder) -> None:
    payload = payload_builder()

    artifact = parse_factory_workflow_artifact(payload)
    recovered = parse_factory_workflow_artifact(
        artifact.model_dump(mode="json", by_alias=True)
    )

    assert recovered == artifact
    assert artifact.schema_name == payload["schema"]


def test_gateway_workflow_sequence_requires_current_phase_and_prior_artifact() -> None:
    factory_job = job()
    inventory = parse_factory_workflow_artifact(inventory_payload())
    brief = parse_factory_workflow_artifact(brief_payload())
    execution = parse_factory_workflow_artifact(execution_payload())
    evaluation = parse_factory_workflow_artifact(evaluation_payload())
    feedback = parse_factory_workflow_artifact(feedback_payload())
    forge_requested = FactoryProjection.from_job(factory_job).model_copy(
        update={
            "status": FactoryLifecycleStatus.RUNNING,
            "phase": FactoryPhase.FORGE_REQUESTED,
        }
    )
    blueprint_created = forge_requested.model_copy(
        update={"phase": FactoryPhase.BLUEPRINT_CREATED}
    )

    GatewayStore._assert_workflow_sequence(forge_requested, inventory, ())
    with pytest.raises(HTTPException, match="prior workflow artifact"):
        GatewayStore._assert_workflow_sequence(blueprint_created, brief, ())
    GatewayStore._assert_workflow_sequence(
        blueprint_created, brief, (inventory,)
    )
    GatewayStore._assert_workflow_sequence(
        blueprint_created.model_copy(update={"phase": FactoryPhase.BUILD_PASSED}),
        execution,
        (inventory, brief),
    )
    GatewayStore._assert_workflow_sequence(
        blueprint_created.model_copy(
            update={"phase": FactoryPhase.REAL_CASE_EVIDENCE}
        ),
        evaluation,
        (inventory, brief, execution),
    )
    GatewayStore._assert_workflow_sequence(
        blueprint_created.model_copy(
            update={"phase": FactoryPhase.QUALITY_REVIEWED}
        ),
        feedback,
        (inventory, brief, execution, evaluation),
    )
    with pytest.raises(HTTPException, match="current factory phase"):
        GatewayStore._assert_workflow_sequence(
            blueprint_created, inventory, ()
        )


def test_gateway_workflow_sequence_rejects_improvement_on_first_attempt() -> None:
    factory_job = job()
    revision = parse_factory_workflow_artifact(revision_payload())
    improvement_requested = FactoryProjection.from_job(factory_job).model_copy(
        update={
            "status": FactoryLifecycleStatus.RUNNING,
            "phase": FactoryPhase.IMPROVEMENT_REQUESTED,
        }
    )

    assert revision.invocation.step is FactorySkillStep.IMPROVE_TEAM
    with pytest.raises(HTTPException, match="later attempt"):
        GatewayStore._assert_workflow_sequence(
            improvement_requested, revision, ()
        )

    discover = parse_factory_workflow_artifact(inventory_payload())
    discover_invocation = discover.invocation.model_copy(update={"attempt": 2})
    discover = discover.model_copy(
        update={"attempt": 2, "invocation": discover_invocation}
    )
    revision_invocation = revision.invocation.model_copy(update={"attempt": 2})
    revision = revision.model_copy(
        update={"attempt": 2, "invocation": revision_invocation}
    )
    GatewayStore._assert_workflow_sequence(
        improvement_requested.model_copy(update={"attempt": 2}),
        revision,
        (discover,),
    )


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


def _release_evidence() -> tuple[E2ERunEvidence, ...]:
    return (
        E2ERunEvidence(
            run_number=1,
            correlation_id=job().correlation_id,
            kind=E2EKind.RECOVERY,
            outcome=E2EOutcome.EXPECTED_FAILURE,
            evidence_ref=ArtifactRef.model_validate(artifact("recovery-e2e")),
        ),
        *(
            E2ERunEvidence(
                run_number=number,
                correlation_id=job().correlation_id,
                kind=E2EKind.NORMAL,
                outcome=E2EOutcome.SUCCEEDED,
                evidence_ref=ArtifactRef.model_validate(artifact(f"normal-e2e-{number}")),
            )
            for number in range(2, 5)
        ),
    )


def test_gateway_recomputes_the_factory_release_decision_before_acceptance() -> None:
    validator = getattr(GatewayStore, "_assert_factory_release_decision", None)
    assert validator is not None, "Gateway release-decision validator is missing"
    evaluation = accepted_evaluation()
    decision = accepted_release_decision(evaluation)

    validator(job(), evaluation, _release_evidence(), decision)

    with pytest.raises(HTTPException, match="recomputed"):
        validator(job(), evaluation, _release_evidence()[:-1], decision)


def test_factory_release_decision_submission_is_typed_and_strict() -> None:
    submission_type = getattr(
        gateway_contracts,
        "FactoryReleaseDecisionSubmission",
        None,
    )
    assert submission_type is not None, "Factory release submission contract is missing"
    payload = {
        "decision": accepted_release_decision(),
        "e2e_evidence": _release_evidence(),
    }

    submission = submission_type.model_validate(payload)

    assert submission.decision.status == "ready"
    assert len(submission.e2e_evidence) == 4
    with pytest.raises(ValidationError):
        submission_type.model_validate({**payload, "unexpected": True})

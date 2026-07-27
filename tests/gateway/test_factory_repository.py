from __future__ import annotations

import hashlib
import json
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import gateway.contracts as gateway_contracts

from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.business_benchmark_contracts import BusinessBenchmarkSummaryV1
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.release_gate import E2EKind, E2EOutcome, E2ERunEvidence
from agenten.agent_factory.service import (
    FactoryCoordinator,
    FactoryRepositoryError,
    InMemoryFactoryRepository,
)
from agenten.agent_factory.skill_evaluation import HermesSkillEvaluationEvidence
from agenten.agent_factory.skill_workflow_contracts import (
    FACTORY_SKILL_ID_BY_STEP,
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
from gateway.app import CAPTAIN_SKILL_EVENT_TYPES, require_skill_event_writer
from gateway.auth import GatewayRole
from agenten.agent_factory.business_benchmark_contracts import canonical_business_benchmark_model_bytes
from tests.agent_factory.test_state_machine import (
    accepted_evaluation,
    accepted_release_decision,
    artifact,
    block,
    job,
)
from tests.agent_factory.test_execution_budget import job_v3
from tests.agent_factory.test_release_gate import (
    workflow_budget,
    workflow_benchmark,
    workflow_evaluation,
    workflow_job,
    workflow_receipts,
    workflow_run,
)
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
        self.budgets = {}
        self.usage_receipts = {}
        self.released_skills = []
        self.skill_assignments = []
        self.benchmark_summaries = {}

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

    def factory_budget(self, job_id):
        return self.budgets[job_id]

    def factory_usage_receipts(self, job_id):
        return tuple(self.usage_receipts.get(job_id, ()))

    def record_released_factory_skill(self, skill):
        self.released_skills.append(skill)
        return type("Receipt", (), {"replayed": False})()

    def record_factory_skill_assignment(self, assignment):
        self.skill_assignments.append(assignment)
        return type("Receipt", (), {"replayed": False})()

    def factory_skill_assignment(self, job_id, step):
        return next(
            assignment
            for assignment in self.skill_assignments
            if assignment.job_id == job_id and assignment.step is step
        )

    def record_business_benchmark_summary(self, summary):
        self.benchmark_summaries[summary.summary_id] = summary
        return type("Receipt", (), {"replayed": False})()

    def business_benchmark_summary(self, summary_id):
        return self.benchmark_summaries.get(summary_id)

    def business_benchmark_summary_by_artifact(self, artifact_ref):
        return next(
            (item for item in self.benchmark_summaries.values() if item.artifact_ref == artifact_ref),
            None,
        )


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


def test_gateway_repository_exposes_budget_and_usage_as_read_only_evidence(job_v3) -> None:
    store = Store()
    budget = FactoryBudgetProjection(
        job_id=job_v3.job_id,
        limit_usd=job_v3.execution_policy.max_cost_usd,
        consumed_usd="0",
        reserved_usd="0",
        remaining_usd=job_v3.execution_policy.max_cost_usd,
    )
    store.budgets[job_v3.job_id] = budget
    store.usage_receipts[job_v3.job_id] = []
    repository = GatewayFactoryRepository(store)

    assert repository.workflow_budget_projection(job_v3.job_id) == budget
    assert repository.workflow_usage_receipts(job_v3.job_id) == ()


def test_gateway_repository_records_and_resolves_business_benchmark_summary() -> None:
    runs = (workflow_run(1),)
    summary = workflow_benchmark(runs)
    store = Store()
    repository = GatewayFactoryRepository(store)

    assert repository.record_business_benchmark_summary(summary) is True
    assert repository.business_benchmark_summary(summary.summary_id) == summary
    assert repository.business_benchmark_summary_by_artifact(summary.artifact_ref) == summary


def test_gateway_repository_translates_benchmark_conflict() -> None:
    class ConflictStore(Store):
        def record_business_benchmark_summary(self, summary):
            raise HTTPException(status_code=409, detail="benchmark conflict")

    with pytest.raises(FactoryRepositoryError, match="benchmark conflict"):
        GatewayFactoryRepository(ConflictStore()).record_business_benchmark_summary(
            workflow_benchmark((workflow_run(1),))
        )


def test_business_benchmark_delivery_event_is_deterministic_and_captain_only() -> None:
    summary = workflow_benchmark((workflow_run(1),))
    content_sha256 = hashlib.sha256(
        canonical_business_benchmark_model_bytes(summary)
    ).hexdigest()

    first = GatewayStore._business_benchmark_validated_event(summary, content_sha256)
    replay = GatewayStore._business_benchmark_validated_event(summary, content_sha256)

    assert first == replay
    assert first.event_type in CAPTAIN_SKILL_EVENT_TYPES
    require_skill_event_writer(first, GatewayRole.CAPTAIN)
    with pytest.raises(HTTPException) as denied:
        require_skill_event_writer(first, GatewayRole.WORKER)
    assert denied.value.status_code == 403
    assert "case_metrics" not in first.model_dump_json()


def test_gateway_repository_seeds_all_six_exact_job_skill_assignments() -> None:
    factory_job = workflow_job(mode="release")
    base_skill = parse_factory_workflow_artifact(
        inventory_payload()
    ).invocation.released_skill

    class Catalog:
        def released_for(self, _job, step: FactorySkillStep):
            digest = hashlib.sha256(step.value.encode()).hexdigest()
            return base_skill.model_copy(
                update={
                    "skill_id": FACTORY_SKILL_ID_BY_STEP[step],
                    "content_ref": base_skill.content_ref.model_copy(
                        update={
                            "uri": (
                                "artifact://released-skills/"
                                f"{FACTORY_SKILL_ID_BY_STEP[step]}/v1"
                            ),
                            "sha256": digest,
                        }
                    ),
                    "content_sha256": digest,
                }
            )

    store = Store()
    repository = GatewayFactoryRepository(store)
    repository.register(factory_job)
    repository.seed_released_skill_assignments(factory_job, Catalog())

    assert tuple(item.step for item in store.skill_assignments) == tuple(
        FactorySkillStep
    )
    assert tuple(
        item.released_skill.skill_id for item in store.skill_assignments
    ) == tuple(FACTORY_SKILL_ID_BY_STEP.values())
    assert tuple(item.released_skill for item in store.skill_assignments) == tuple(
        store.released_skills
    )
    assert repository.released_for(
        factory_job,
        FactorySkillStep.DISCOVER,
    ) == store.skill_assignments[0].released_skill

    changed_envelope = factory_job.model_copy(
        update={"max_behavioral_iterations": 4}
    )
    with pytest.raises(FactoryRepositoryError, match="job envelope"):
        repository.released_for(changed_envelope, FactorySkillStep.DISCOVER)


def test_gateway_release_fails_closed_until_task6_resolves_benchmark_summary() -> None:
    runs = tuple(workflow_run(number) for number in range(1, 4))
    evaluation = workflow_evaluation(runs)
    store = GatewayStore.__new__(GatewayStore)
    store._factory_workflow_artifacts_for_job = (  # type: ignore[method-assign]
        lambda _cursor, _job_id, *, for_update: (*runs, evaluation)
    )
    store._factory_budget_projection = (  # type: ignore[method-assign]
        lambda _cursor, _job, *, for_update: workflow_budget()
    )
    store._factory_usage_receipts_for_job = (  # type: ignore[method-assign]
        lambda _cursor, _job_id, *, for_update: workflow_receipts(runs)
    )
    store._factory_business_benchmark_summary_by_artifact = (  # type: ignore[method-assign]
        lambda _cursor, _artifact_ref, *, for_update: None
    )

    decision = store._factory_workflow_release_decision(
        object(),
        workflow_job(mode="release"),
        attempt=1,
        evaluation=evaluation,
        for_update=True,
    )

    assert decision is not None
    assert decision.status == "blocked"
    assert decision.reasons == (
        "missing authoritative business benchmark summary",
    )
    assert decision.evaluation_ref == evaluation.artifact_ref


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
    second_execution = execution.model_copy(
        update={
            "run_number": 2,
            "invocation_id": UUID("00000000-0000-0000-0000-000000000381"),
            "invocation": execution.invocation.model_copy(
                update={
                    "invocation_id": UUID(
                        "00000000-0000-0000-0000-000000000381"
                    ),
                    "idempotency_key": "8" * 64,
                    "execution_scope_ref": execution.holdout_ref.model_copy(
                        update={
                            "holdout_id": "holdout-333333333333",
                            "uri": "holdout://holdout-333333333333",
                            "sha256": "3" * 64,
                        }
                    ),
                }
            ),
            "holdout_ref": execution.holdout_ref.model_copy(
                update={
                    "holdout_id": "holdout-333333333333",
                    "uri": "holdout://holdout-333333333333",
                    "sha256": "3" * 64,
                }
            ),
        }
    )
    third_execution = second_execution.model_copy(
        update={
            "run_number": 3,
            "invocation_id": UUID("00000000-0000-0000-0000-000000000382"),
            "invocation": second_execution.invocation.model_copy(
                update={
                    "invocation_id": UUID(
                        "00000000-0000-0000-0000-000000000382"
                    ),
                    "idempotency_key": "9" * 64,
                    "execution_scope_ref": second_execution.holdout_ref.model_copy(
                        update={
                            "holdout_id": "holdout-444444444444",
                            "uri": "holdout://holdout-444444444444",
                            "sha256": "4" * 64,
                        }
                    ),
                }
            ),
            "holdout_ref": second_execution.holdout_ref.model_copy(
                update={
                    "holdout_id": "holdout-444444444444",
                    "uri": "holdout://holdout-444444444444",
                    "sha256": "4" * 64,
                }
            ),
        }
    )
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
        blueprint_created.model_copy(update={"phase": FactoryPhase.BUILD_PASSED}),
        second_execution,
        (inventory, brief, execution),
    )
    GatewayStore._assert_workflow_sequence(
        blueprint_created.model_copy(update={"phase": FactoryPhase.BUILD_PASSED}),
        third_execution,
        (inventory, brief, execution, second_execution),
    )
    GatewayStore._assert_workflow_sequence(
        blueprint_created.model_copy(
            update={"phase": FactoryPhase.REAL_CASE_EVIDENCE}
        ),
        evaluation,
        (inventory, brief, execution, second_execution, third_execution),
    )
    feedback_invocation = feedback.invocation.model_copy(
        update={
            "input_ref": evaluation.artifact_ref,
            "input_sha256": evaluation.artifact_ref.sha256,
        }
    )
    feedback = feedback.model_copy(update={"invocation": feedback_invocation})
    GatewayStore._assert_workflow_sequence(
        blueprint_created.model_copy(
            update={"phase": FactoryPhase.REAL_CASE_EVIDENCE}
        ),
        feedback,
        (inventory, brief, execution, second_execution, third_execution, evaluation),
    )
    with pytest.raises(HTTPException, match="current factory phase"):
        GatewayStore._assert_workflow_sequence(
            blueprint_created, inventory, ()
        )


def test_gateway_workflow_input_binding_uses_the_exact_step_predecessor() -> None:
    inventory = parse_factory_workflow_artifact(inventory_payload())
    evaluation = parse_factory_workflow_artifact(evaluation_payload())
    feedback = parse_factory_workflow_artifact(feedback_payload())
    factory_job = job().model_copy(
        update={
            "job_id": inventory.job_id,
            "correlation_id": inventory.correlation_id,
            "subject_version": inventory.subject_version,
            "input_ref": inventory.invocation.input_ref,
            "acceptance_assertion_ids": inventory.acceptance_assertion_ids,
            "required_capability": inventory.invocation.released_skill.capability,
        }
    )
    report_invocation = feedback.invocation.model_copy(
        update={
            "input_ref": evaluation.artifact_ref,
            "input_sha256": evaluation.artifact_ref.sha256,
        }
    )
    feedback = feedback.model_copy(update={"invocation": report_invocation})

    GatewayStore._assert_workflow_artifact(
        factory_job,
        inventory,
        (),
    )
    GatewayStore._assert_workflow_artifact(
        factory_job,
        feedback,
        (evaluation,),
    )

    stale = feedback.model_copy(
        update={
            "invocation": report_invocation.model_copy(
                update={
                    "input_ref": factory_job.input_ref,
                    "input_sha256": factory_job.input_ref.sha256,
                }
            )
        }
    )
    with pytest.raises(HTTPException, match="input binding"):
        GatewayStore._assert_workflow_artifact(
            factory_job,
            stale,
            (evaluation,),
        )


def test_gateway_rejects_foreign_skill_with_the_same_factory_capability() -> None:
    inventory = parse_factory_workflow_artifact(inventory_payload())
    factory_job = job().model_copy(
        update={
            "job_id": inventory.job_id,
            "correlation_id": inventory.correlation_id,
            "subject_version": inventory.subject_version,
            "input_ref": inventory.invocation.input_ref,
            "acceptance_assertion_ids": inventory.acceptance_assertion_ids,
            "required_capability": inventory.invocation.released_skill.capability,
        }
    )
    foreign_skill = inventory.invocation.released_skill.model_copy(
        update={"skill_id": "captain-factory-foreign"}
    )
    foreign_invocation = inventory.invocation.model_copy(
        update={"released_skill": foreign_skill}
    )
    foreign_inventory = inventory.model_copy(
        update={"invocation": foreign_invocation}
    )

    with pytest.raises(HTTPException, match="skill ID"):
        GatewayStore._assert_workflow_artifact(
            factory_job,
            foreign_inventory,
            (),
        )


def test_gateway_requires_exact_persisted_job_step_skill_assignment() -> None:
    assignment_type = getattr(
        gateway_contracts,
        "FactorySkillAssignmentV1",
        None,
    )
    assert assignment_type is not None, "Gateway skill assignment contract is missing"
    validator = getattr(GatewayStore, "_assert_workflow_skill_assignment", None)
    assert validator is not None, "Gateway skill assignment validator is missing"
    inventory = parse_factory_workflow_artifact(inventory_payload())
    assignment = assignment_type(
        job_id=inventory.job_id,
        step=FactorySkillStep.DISCOVER,
        released_skill=inventory.invocation.released_skill,
    )

    class Cursor:
        def __init__(self, payload) -> None:
            self.payload = payload

        def execute(self, *_args) -> None:
            return None

        def fetchone(self):
            return None if self.payload is None else {"payload": self.payload}

    cursor = Cursor(assignment.model_dump(mode="json", by_alias=True))
    validator(GatewayStore.__new__(GatewayStore), cursor, inventory)

    alternate_skill = inventory.invocation.released_skill.model_copy(
        update={
            "version": 2,
            "content_ref": inventory.invocation.released_skill.content_ref.model_copy(
                update={"sha256": "f" * 64}
            ),
            "content_sha256": "f" * 64,
        }
    )
    alternate = inventory.model_copy(
        update={
            "invocation": inventory.invocation.model_copy(
                update={"released_skill": alternate_skill}
            )
        }
    )
    with pytest.raises(HTTPException, match="skill assignment"):
        validator(GatewayStore.__new__(GatewayStore), cursor, alternate)
    with pytest.raises(HTTPException, match="skill assignment"):
        validator(GatewayStore.__new__(GatewayStore), Cursor(None), inventory)


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

    failed_evaluation = parse_factory_workflow_artifact(evaluation_payload()).model_copy(
        update={
            "failure_class": "behavioral_failure",
            "recommendation": "RETRY_BUILD",
        }
    )
    revision_invocation = revision.invocation.model_copy(update={"attempt": 2})
    revision = revision.model_copy(
        update={"attempt": 2, "invocation": revision_invocation}
    )
    retry_projection = improvement_requested.model_copy(update={"attempt": 2})
    retry_discovery = parse_factory_workflow_artifact(inventory_payload())
    retry_discovery = retry_discovery.model_copy(
        update={
            "attempt": 2,
            "invocation": retry_discovery.invocation.model_copy(
                update={"attempt": 2}
            ),
        }
    )
    with pytest.raises(HTTPException, match="current factory phase"):
        GatewayStore._assert_workflow_sequence(
            retry_projection, retry_discovery, (failed_evaluation,)
        )
    with pytest.raises(HTTPException, match="failed evaluation"):
        GatewayStore._assert_workflow_sequence(retry_projection, revision, ())
    GatewayStore._assert_workflow_sequence(
        retry_projection,
        revision,
        (failed_evaluation,),
    )

    brief = parse_factory_workflow_artifact(brief_payload())
    brief_invocation = brief.invocation.model_copy(update={"attempt": 2})
    brief = brief.model_copy(
        update={"attempt": 2, "invocation": brief_invocation}
    )
    GatewayStore._assert_workflow_sequence(
        retry_projection.model_copy(update={"phase": FactoryPhase.BLUEPRINT_CREATED}),
        brief,
        (failed_evaluation, revision),
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

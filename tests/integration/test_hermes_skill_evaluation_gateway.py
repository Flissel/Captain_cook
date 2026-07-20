from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from agenten.agent_factory.candidate_evaluation import (
    FactoryCandidateManifest,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.contracts import (
    AgentFactoryJob,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryLease,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.evidence_store import FilesystemSkillEvaluationEvidenceStore
from agenten.agent_factory.leases import (
    FactoryLeaseDenied,
    issue_factory_lease,
    validate_factory_lease,
)
from agenten.agent_factory.orchestration import HermesSkillEvaluationCoordinator
from agenten.agent_factory.release_gate import (
    E2EKind,
    E2EOutcome,
    E2ERunEvidence,
    evaluate_factory_release,
)
from agenten.agent_factory.service import FactoryCoordinator, FactoryRepositoryError
from agenten.agent_factory.skill_evaluation import (
    HermesSkillEvaluationEvidence,
    HermesSkillEvaluationRequest,
    ReleasedHermesSkill,
    ToolGapMarker,
)
from agenten.agent_factory.skill_store import (
    InMemorySkillEvaluationRepository,
    SkillEvaluationStore,
    StoredSkillEvaluation,
)
from agenten.agent_factory.state_machine import FactoryLifecycleError, FactoryLifecycleStatus
from agenten.agent_runtime.contracts import ArtifactRef
from blockchain.mariadb_storage import MariaDBStorage
from gateway.contracts import (
    FactoryReleaseDecisionSubmission,
    FactorySkillEvaluationSubmission,
    FactoryToolGapReference,
    PublishedHermesSkill,
)
from gateway.factory_repository import GatewayFactoryRepository
from gateway.store import GatewayStore
from tests.agent_factory.test_hermes_skill_evaluation import (
    CandidateStore,
    Clock,
    Evaluator,
    Hermes,
    _candidate,
    _evidence,
)
from tests.agent_factory.test_skill_evaluation_contracts import NOW, gap_payload
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")


def test_captain_skill_evaluation_operational_chain_is_explicit() -> None:
    """Task 7 must expose one auditable Captain-owned operational chain."""

    architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    normalized_architecture = " ".join(architecture.split())

    assert "## Hermes skill-evaluation release path" in architecture
    assert (
        "request -> skill usage -> build/test evidence -> candidate retained "
        "-> Gateway validation -> skill published -> ready-to-use promotion"
        in architecture
    )
    assert "### Verification command sequence" in architecture
    assert (
        ".\\.venv\\Scripts\\python.exe -m pytest -q --no-cov "
        "tests/integration/test_hermes_skill_evaluation_gateway.py"
        in architecture
    )
    assert (
        "`TEST_MARIADB_DSN` must target the exact isolated database "
        "`captain_test`"
        in normalized_architecture
    )
    assert "pwsh -NoProfile -File scripts/run-gate-e.ps1" in architecture
    assert (
        "Missing prerequisites are a skip or block, never a green Gate E."
        in normalized_architecture
    )
    assert (
        "Gate E verifies the generic provider-backed delivery release policy"
        in normalized_architecture
    )
    assert (
        "does not execute the Task 7 Hermes skill fixture or Factory promotion chain"
        in normalized_architecture
    )


@dataclass(frozen=True)
class _EvaluationBundle:
    job: AgentFactoryJob
    candidate: FactoryCandidateManifest
    request: HermesSkillEvaluationRequest
    released_skill: ReleasedHermesSkill
    lease: FactoryLease
    stored: StoredSkillEvaluation
    submission: FactorySkillEvaluationSubmission


class _DeterministicGatewayStore:
    """In-memory Gateway shape that delegates qualification to real Gateway rules."""

    def __init__(self) -> None:
        self.jobs: dict[UUID, AgentFactoryJob] = {}
        self.blocks: dict[UUID, list[FactoryEvidenceBlock]] = {}
        self.leases: dict[str, FactoryLease] = {}
        self.released_skills: dict[tuple[str, int], ReleasedHermesSkill] = {}
        self.submissions: dict[UUID, FactorySkillEvaluationSubmission] = {}
        self.evaluations: dict[UUID, StoredSkillEvaluation] = {}
        self.publications: dict[UUID, PublishedHermesSkill] = {}
        self.release_decisions: dict[UUID, list[FactoryReleaseDecisionSubmission]] = {}

    def record_factory_job(self, job: AgentFactoryJob) -> SimpleNamespace:
        existing = self.jobs.get(job.job_id)
        if existing is not None and existing != job:
            raise HTTPException(status_code=409, detail="factory job conflict")
        replayed = existing is not None
        self.jobs[job.job_id] = job
        self.blocks.setdefault(job.job_id, [])
        return SimpleNamespace(replayed=replayed)

    def factory_job(self, job_id: UUID) -> SimpleNamespace:
        return SimpleNamespace(
            job=self.jobs[job_id],
            blocks=tuple(self.blocks.get(job_id, ())),
            leases=tuple(self.leases.values()),
        )

    def record_factory_lease(self, lease: FactoryLease) -> SimpleNamespace:
        self.leases[lease.lease_id] = lease
        return SimpleNamespace(replayed=False)

    def record_released_factory_skill(self, skill: ReleasedHermesSkill) -> SimpleNamespace:
        self.released_skills[(skill.skill_id, skill.version)] = skill
        return SimpleNamespace(replayed=False)

    def record_factory_skill_evaluation(
        self,
        submission: FactorySkillEvaluationSubmission,
    ) -> SimpleNamespace:
        evidence = submission.evidence
        job = self.jobs[evidence.job_id]
        GatewayStore._assert_factory_evaluation_job(evidence, job)
        recorded_lease = self.leases.get(evidence.request.lease.lease_id)
        if recorded_lease != evidence.request.lease:
            raise HTTPException(
                status_code=409,
                detail="missing matching active factory lease for skill evaluation",
            )
        try:
            validate_factory_lease(
                recorded_lease,
                job=job,
                role=FactoryRole.TOOL_INTEGRATOR,
                attempt=recorded_lease.attempt,
                now=evidence.occurred_at,
            )
        except FactoryLeaseDenied as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        released = self.released_skills.get(
            (evidence.request.released_skill.skill_id, evidence.request.released_skill.version)
        )
        if released != evidence.request.released_skill:
            raise HTTPException(
                status_code=409,
                detail="skill evaluation references an unknown released skill",
            )
        GatewayStore._assert_evaluation_references(submission)
        stored = GatewayStore._stored_factory_evaluation(submission)
        self.submissions[evidence.evidence_id] = submission
        self.evaluations[evidence.job_id] = stored
        return SimpleNamespace(replayed=False)

    def factory_skill_evaluation(self, job_id: UUID) -> StoredSkillEvaluation | None:
        return self.evaluations.get(job_id)

    def record_factory_release_decision(
        self,
        submission: FactoryReleaseDecisionSubmission,
    ) -> SimpleNamespace:
        job_id = submission.decision.job_id
        evaluation = self.evaluations[job_id]
        GatewayStore._assert_factory_release_decision(
            self.jobs[job_id],
            evaluation,
            submission.e2e_evidence,
            submission.decision,
        )
        assert_recordable = getattr(
            GatewayStore,
            "_assert_factory_release_decision_recordable",
            None,
        )
        if assert_recordable is not None:
            projection = FactoryCoordinator(
                GatewayFactoryRepository(self)
            ).projection(job_id)
            assert_recordable(projection)
        self.release_decisions.setdefault(job_id, []).append(submission)
        return SimpleNamespace(replayed=False)

    def factory_release_decision(self, job_id: UUID):
        submissions = self.release_decisions.get(job_id, ())
        return None if not submissions else submissions[-1].decision

    def publish_factory_skill(self, publication: PublishedHermesSkill) -> SimpleNamespace:
        submission = self.submissions[publication.evaluation_id]
        GatewayStore._assert_publication_qualification(
            publication,
            submission,
            self.jobs[submission.evidence.job_id],
        )
        self.publications[publication.evaluation_id] = publication
        return SimpleNamespace(replayed=False)

    def record_factory_block(self, block: FactoryEvidenceBlock) -> SimpleNamespace:
        if block.phase is FactoryPhase.CAPABILITY_PROMOTED:
            evaluation = self.evaluations.get(block.job_id)
            if (
                evaluation is None
                or evaluation.evidence.evidence_id not in self.publications
            ):
                raise HTTPException(
                    status_code=409,
                    detail="capability promotion requires a Captain-published skill",
                )
            if evaluation.evidence_ref not in block.evidence_refs:
                raise HTTPException(
                    status_code=409,
                    detail="capability promotion must reference its accepted skill evaluation",
                )
        self.blocks.setdefault(block.job_id, []).append(block)
        return SimpleNamespace(replayed=False)


def _artifact(name: str, content: bytes, media_type: str = "application/json") -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://task-7/{name}",
        sha256=hashlib.sha256(content).hexdigest(),
        media_type=media_type,
    )


def _canonical_ref(model: BaseModel, name: str) -> ArtifactRef:
    content = json.dumps(
        model.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _artifact(name, content)


def _job(candidate: FactoryCandidateManifest) -> AgentFactoryJob:
    return AgentFactoryJob(
        schema_name="captain.agent-factory-job.v1",
        event_id=UUID("00000000-0000-0000-0000-000000000701"),
        correlation_id=UUID("00000000-0000-0000-0000-000000000702"),
        occurred_at=NOW,
        producer="captain",
        job_id=UUID("00000000-0000-0000-0000-000000000703"),
        subject_version=1,
        input_ref=candidate.source_archive_ref,
        required_capability="support_triage",
        acceptance_assertion_ids=("schema_valid", "real_case_green"),
    )


def _released_skill(tmp_path: Path, job: AgentFactoryJob) -> ReleasedHermesSkill:
    skill_path = tmp_path / "released-skill.md"
    skill_path.write_text(
        "# Released support-triage evaluator\n\nUse the sealed candidate and report typed evidence.\n",
        encoding="utf-8",
    )
    reference = _artifact(
        "released-support-triage-skill",
        skill_path.read_bytes(),
        "text/markdown",
    )
    return ReleasedHermesSkill(
        schema_name="captain.released-hermes-skill.v1",
        skill_id="support_triage_evaluator",
        version=1,
        capability=job.required_capability,
        content_ref=reference,
        content_sha256=reference.sha256,
        status="released",
        released_at=NOW - timedelta(minutes=1),
        producer="captain",
    )


def _request(
    job: AgentFactoryJob,
    candidate: FactoryCandidateManifest,
    lease: FactoryLease,
    released_skill: ReleasedHermesSkill,
    *,
    max_iterations: int = 3,
) -> HermesSkillEvaluationRequest:
    return HermesSkillEvaluationRequest(
        schema_name="captain.hermes-skill-evaluation-request.v1",
        request_id=uuid5(NAMESPACE_URL, f"task-7-request|{lease.lease_id}|{max_iterations}"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_id=job.required_capability,
        subject_version=job.subject_version,
        occurred_at=lease.issued_at,
        producer="captain",
        lease=lease,
        released_skill=released_skill,
        candidate_source_ref=candidate.source_archive_ref,
        acceptance_assertion_ids=job.acceptance_assertion_ids,
        max_iterations=max_iterations,
    )


def _tool_gaps(mode: str) -> tuple[ToolGapMarker, ...]:
    if mode == "none":
        return ()
    optional = ToolGapMarker.model_validate(
        gap_payload(
            gap_id="optional-observability",
            severity="optional",
            status="unresolved",
        )
    )
    if mode == "required-open":
        return (
            ToolGapMarker.model_validate(
                gap_payload(
                    gap_id="required-runtime-tool",
                    severity="required",
                    status="unresolved",
                )
            ),
            optional,
        )
    return (
        ToolGapMarker.model_validate(
            gap_payload(
                gap_id="required-runtime-tool",
                severity="required",
                status="resolved",
            )
        ),
        optional,
    )


def _submission(evidence: HermesSkillEvaluationEvidence) -> FactorySkillEvaluationSubmission:
    return FactorySkillEvaluationSubmission(
        evidence=evidence,
        evidence_ref=_canonical_ref(evidence, f"evaluation-{evidence.evidence_id}"),
        receipt_ref=_canonical_ref(evidence.receipt, f"receipt-{evidence.receipt.receipt_id}"),
        candidate_ref=None if evidence.candidate is None else evidence.candidate.content_ref,
        tool_gap_refs=tuple(
            FactoryToolGapReference(gap_id=marker.gap_id, evidence_ref=marker.evidence_ref)
            for marker in evidence.tool_gaps
        ),
    )


async def _evaluate_bundle(
    tmp_path: Path,
    *,
    build_fails: bool = False,
    gap_mode: str = "mixed",
    before_evaluate: Callable[
        [AgentFactoryJob, ReleasedHermesSkill, FactoryLease], None
    ]
    | None = None,
) -> _EvaluationBundle:
    candidate, archive = _candidate(tmp_path, build_fails=build_fails)
    job = _job(candidate)
    lease = issue_factory_lease(
        job=job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/task-7/tooling",
        now=NOW,
    )
    released_skill = _released_skill(tmp_path, job)
    request = _request(job, candidate, lease, released_skill)
    if before_evaluate is not None:
        before_evaluate(job, released_skill, lease)
    proposed = _evidence(request, attempt=1).model_copy(
        update={"tool_gaps": _tool_gaps(gap_mode)}
    )
    private_store = SkillEvaluationStore(
        repository=InMemorySkillEvaluationRepository(),
        evidence_store=FilesystemSkillEvaluationEvidenceStore(tmp_path / "evidence"),
    )
    result = await HermesSkillEvaluationCoordinator(
        cli=Hermes([proposed]),
        evaluator=Evaluator(),
        candidate_store=CandidateStore(
            [ResolvedFactoryCandidate(candidate=candidate, source_archive=archive)]
        ),
        private_store=private_store,
        clock=Clock(NOW + timedelta(minutes=1, seconds=30)),
    ).evaluate(request)
    stored = private_store.get_evaluation(result.evidence.evidence_id)
    assert stored is not None
    return _EvaluationBundle(
        job=job,
        candidate=candidate,
        request=request,
        released_skill=released_skill,
        lease=lease,
        stored=stored,
        submission=_submission(result.evidence),
    )


def _block(
    job: AgentFactoryJob,
    phase: FactoryPhase,
    *,
    lease: FactoryLease | None = None,
    assertions: tuple[str, ...] = (),
) -> FactoryEvidenceBlock:
    role_for_phase = {
        FactoryPhase.BLUEPRINT_CREATED: FactoryRole.AGENT_ARCHITECT,
        FactoryPhase.TOOL_CANDIDATE_TESTED: FactoryRole.TOOL_INTEGRATOR,
        FactoryPhase.AGENT_CODE_CREATED: FactoryRole.TOOL_INTEGRATOR,
        FactoryPhase.BUILD_PASSED: FactoryRole.TOOL_INTEGRATOR,
        FactoryPhase.REAL_CASE_EVIDENCE: FactoryRole.REAL_CASE_TESTER,
        FactoryPhase.QUALITY_REVIEWED: FactoryRole.QUALITY_WARDEN,
    }.get(phase)
    producer = "hermes" if role_for_phase is not None else "captain"
    evidence_ref = _artifact(f"lifecycle-{phase.value}", phase.value.encode("utf-8"))
    return FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id=uuid5(NAMESPACE_URL, f"task-7-block|{job.job_id}|{phase.value}"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        causation_id=job.event_id,
        occurred_at=NOW + timedelta(minutes=2),
        producer=producer,
        subject_version=job.subject_version,
        attempt=1,
        phase=phase,
        role=role_for_phase,
        status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=(evidence_ref,),
        evidence_refs=(evidence_ref,),
        assertion_ids=assertions,
        lease_id=None if lease is None else lease.lease_id,
    )


def _record_evaluation_prerequisites(
    coordinator: FactoryCoordinator,
    store: _DeterministicGatewayStore | GatewayStore,
    job: AgentFactoryJob,
    released_skill: ReleasedHermesSkill,
    lease: FactoryLease,
) -> None:
    coordinator.register(job)
    store.record_released_factory_skill(released_skill)
    coordinator.record(_block(job, FactoryPhase.FORGE_REQUESTED))
    architect_lease = issue_factory_lease(
        job=job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/task-7/architecture",
        now=NOW,
    )
    store.record_factory_lease(architect_lease)
    coordinator.record(
        _block(job, FactoryPhase.BLUEPRINT_CREATED, lease=architect_lease)
    )
    store.record_factory_lease(lease)


def _record_evaluation_through_quality(
    coordinator: FactoryCoordinator,
    store: _DeterministicGatewayStore | GatewayStore,
    bundle: _EvaluationBundle,
) -> None:
    store.record_factory_skill_evaluation(bundle.submission)
    for phase in (
        FactoryPhase.TOOL_CANDIDATE_TESTED,
        FactoryPhase.AGENT_CODE_CREATED,
        FactoryPhase.BUILD_PASSED,
    ):
        coordinator.record(_block(bundle.job, phase, lease=bundle.lease))
    real_case_lease = issue_factory_lease(
        job=bundle.job,
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        workspace_ref="workspace://factory/task-7/real-case",
        now=NOW,
    )
    store.record_factory_lease(real_case_lease)
    coordinator.record(
        _block(
            bundle.job,
            FactoryPhase.REAL_CASE_EVIDENCE,
            lease=real_case_lease,
            assertions=("real_case_green",),
        )
    )
    quality_lease = issue_factory_lease(
        job=bundle.job,
        role=FactoryRole.QUALITY_WARDEN,
        attempt=1,
        workspace_ref="workspace://factory/task-7/quality",
        now=NOW,
    )
    store.record_factory_lease(quality_lease)
    coordinator.record(
        _block(
            bundle.job,
            FactoryPhase.QUALITY_REVIEWED,
            lease=quality_lease,
            assertions=("schema_valid",),
        )
    )


def _publication(bundle: _EvaluationBundle) -> PublishedHermesSkill:
    evidence = bundle.submission.evidence
    assert evidence.candidate is not None
    return PublishedHermesSkill(
        skill_id=bundle.released_skill.skill_id,
        version=bundle.released_skill.version + 1,
        candidate_id=evidence.candidate.candidate_id,
        evaluation_id=evidence.evidence_id,
        content_ref=evidence.candidate.content_ref,
        content_sha256=evidence.candidate.content_sha256,
        published_at=evidence.occurred_at + timedelta(minutes=1),
        producer="captain",
        status="published",
    )


def _promotion(bundle: _EvaluationBundle) -> FactoryEvidenceBlock:
    return _block(
        bundle.job,
        FactoryPhase.CAPABILITY_PROMOTED,
        assertions=bundle.job.acceptance_assertion_ids,
    ).model_copy(
        update={
            "occurred_at": bundle.submission.evidence.occurred_at + timedelta(minutes=2),
            "evidence_refs": (bundle.submission.evidence_ref,),
        }
    )


def _e2e_runs(job: AgentFactoryJob, normal_successes: int) -> tuple[E2ERunEvidence, ...]:
    outcomes = [
        E2ERunEvidence(
            run_number=1,
            correlation_id=job.correlation_id,
            kind=E2EKind.RECOVERY,
            outcome=E2EOutcome.EXPECTED_FAILURE,
            evidence_ref=_artifact("recovery-e2e", b"expected failure"),
        )
    ]
    outcomes.extend(
        E2ERunEvidence(
            run_number=index + 2,
            correlation_id=job.correlation_id,
            kind=E2EKind.NORMAL,
            outcome=E2EOutcome.SUCCEEDED,
            evidence_ref=_artifact(f"normal-e2e-{index + 1}", str(index).encode("ascii")),
        )
        for index in range(normal_successes)
    )
    return tuple(outcomes)


def _release_submission(
    bundle: _EvaluationBundle,
    normal_successes: int,
    evaluation: StoredSkillEvaluation,
) -> FactoryReleaseDecisionSubmission:
    evidence = _e2e_runs(bundle.job, normal_successes)
    return FactoryReleaseDecisionSubmission(
        decision=evaluate_factory_release(bundle.job, evidence, evaluation),
        e2e_evidence=evidence,
    )


@pytest.mark.asyncio
async def test_sealed_candidate_reaches_only_captain_owned_publication_and_promotion(
    tmp_path: Path,
) -> None:
    store = _DeterministicGatewayStore()
    coordinator = FactoryCoordinator(GatewayFactoryRepository(store))
    bundle = await _evaluate_bundle(
        tmp_path,
        gap_mode="mixed",
        before_evaluate=lambda job, skill, lease: _record_evaluation_prerequisites(
            coordinator,
            store,
            job,
            skill,
            lease,
        ),
    )

    _record_evaluation_through_quality(coordinator, store, bundle)

    gateway_evaluation = coordinator.evaluation_for_job(bundle.job.job_id)
    assert gateway_evaluation is not None
    assert bundle.request.producer == "captain"
    assert bundle.stored.evidence.receipt.producer == "hermes"
    assert bundle.stored.evidence.candidate is not None
    assert bundle.stored.evidence.candidate.status == "private_candidate"
    assert bundle.stored.candidate_ref is not None
    assert tuple(check.kind for check in bundle.stored.evidence.checks) == ("build", "test")
    assert all(check.status == "passed" for check in bundle.stored.evidence.checks)
    assert {(gap.severity, gap.status) for gap in gateway_evaluation.tool_gaps} == {
        ("required", "resolved"),
        ("optional", "unresolved"),
    }

    release_submission = _release_submission(bundle, 3, gateway_evaluation)
    release = release_submission.decision
    assert release.status == "ready"

    with pytest.raises(
        FactoryLifecycleError,
        match="missing accepted Factory release decision",
    ):
        coordinator.record(_promotion(bundle))

    blocked_submission = _release_submission(bundle, 2, gateway_evaluation)
    store.record_factory_release_decision(blocked_submission)
    with pytest.raises(FactoryLifecycleError, match="release decision is blocked"):
        coordinator.record(_promotion(bundle))

    store.record_factory_release_decision(release_submission)
    with pytest.raises(
        FactoryRepositoryError,
        match="Captain-published skill",
    ):
        coordinator.record(_promotion(bundle))

    publication = _publication(bundle)
    store.publish_factory_skill(publication)
    coordinator.record(_promotion(bundle))
    projection = coordinator.projection(bundle.job.job_id)

    assert publication.producer == "captain"
    assert _promotion(bundle).producer == "captain"
    assert projection.status is FactoryLifecycleStatus.READY_TO_USE
    assert projection.evaluation_id == gateway_evaluation.evidence.evidence_id
    assert projection.evaluation_ref == gateway_evaluation.evidence_ref

    with pytest.raises(HTTPException, match="sealed after capability promotion"):
        store.record_factory_release_decision(blocked_submission)

    replayed = coordinator.projection(bundle.job.job_id)
    assert replayed.status is FactoryLifecycleStatus.READY_TO_USE


@pytest.mark.asyncio
async def test_altered_released_skill_digest_fails_closed_at_gateway_validation(
    tmp_path: Path,
) -> None:
    bundle = await _evaluate_bundle(tmp_path)
    evidence = bundle.submission.evidence
    changed_ref = bundle.released_skill.content_ref.model_copy(
        update={"sha256": "f" * 64}
    )
    changed_skill = bundle.released_skill.model_copy(
        update={"content_ref": changed_ref, "content_sha256": "f" * 64}
    )
    request = evidence.request.model_copy(update={"released_skill": changed_skill})
    receipt = evidence.receipt.model_copy(
        update={
            "released_skill": changed_skill,
            "used_skill_sha256": changed_skill.content_sha256,
        }
    )
    assert evidence.candidate is not None
    candidate = evidence.candidate.model_copy(
        update={"parent_released_skill": changed_skill}
    )
    changed_evidence = HermesSkillEvaluationEvidence.model_validate(
        evidence.model_copy(
            update={"request": request, "receipt": receipt, "candidate": candidate}
        ).model_dump(mode="json", by_alias=True)
    )
    store = _DeterministicGatewayStore()
    store.record_factory_job(bundle.job)
    store.record_factory_lease(bundle.lease)
    store.record_released_factory_skill(bundle.released_skill)

    with pytest.raises(HTTPException, match="unknown released skill"):
        store.record_factory_skill_evaluation(_submission(changed_evidence))


@pytest.mark.asyncio
async def test_deliberately_failing_build_never_retains_or_publishes_a_candidate(
    tmp_path: Path,
) -> None:
    bundle = await _evaluate_bundle(tmp_path, build_fails=True, gap_mode="none")
    store = _DeterministicGatewayStore()
    store.record_factory_job(bundle.job)
    store.record_factory_lease(bundle.lease)
    store.record_released_factory_skill(bundle.released_skill)
    store.record_factory_skill_evaluation(bundle.submission)

    assert bundle.stored.evidence.outcome == "failed"
    assert bundle.stored.candidate_ref is None
    assert any(
        check.kind == "build" and check.status == "failed"
        for check in bundle.stored.evidence.checks
    )
    with pytest.raises(HTTPException, match="evaluator did not succeed"):
        store.publish_factory_skill(_publication(bundle))


@pytest.mark.asyncio
async def test_unresolved_required_tool_gap_blocks_retention_and_publication(
    tmp_path: Path,
) -> None:
    bundle = await _evaluate_bundle(tmp_path, gap_mode="required-open")
    store = _DeterministicGatewayStore()
    store.record_factory_job(bundle.job)
    store.record_factory_lease(bundle.lease)
    store.record_released_factory_skill(bundle.released_skill)
    store.record_factory_skill_evaluation(bundle.submission)

    assert bundle.stored.evidence.outcome == "blocked_tool_gap"
    assert bundle.stored.candidate_ref is None
    with pytest.raises(HTTPException, match="unresolved required TODO_TOOL"):
        store.publish_factory_skill(_publication(bundle))


@pytest.mark.asyncio
async def test_stale_evaluation_lease_is_rejected_before_gateway_acceptance(
    tmp_path: Path,
) -> None:
    bundle = await _evaluate_bundle(tmp_path)
    stale_evidence = bundle.submission.evidence.model_copy(
        update={"occurred_at": bundle.lease.expires_at}
    )
    store = _DeterministicGatewayStore()
    store.record_factory_job(bundle.job)
    store.record_factory_lease(bundle.lease)
    store.record_released_factory_skill(bundle.released_skill)

    with pytest.raises(ValidationError, match="outside the active lease"):
        _submission(stale_evidence)


@pytest.mark.asyncio
async def test_clean_bounded_retry_creates_fresh_evidence_but_still_needs_release_streak(
    tmp_path: Path,
) -> None:
    candidate, archive = _candidate(tmp_path)
    failing = candidate.model_copy(
        update={"real_case_command": ("python", "-c", "raise SystemExit(1)")}
    )
    job = _job(candidate)
    lease = issue_factory_lease(
        job=job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/task-7/recovery",
        now=NOW,
    )
    request = _request(job, candidate, lease, _released_skill(tmp_path, job), max_iterations=2)
    first_proposal = _evidence(request, attempt=1)
    second_proposal = _evidence(request, attempt=2)
    private_store = SkillEvaluationStore(
        repository=InMemorySkillEvaluationRepository(),
        evidence_store=FilesystemSkillEvaluationEvidenceStore(tmp_path / "retry-evidence"),
    )
    result = await HermesSkillEvaluationCoordinator(
        cli=Hermes([first_proposal, second_proposal]),
        evaluator=Evaluator(),
        candidate_store=CandidateStore(
            [
                ResolvedFactoryCandidate(candidate=failing, source_archive=archive),
                ResolvedFactoryCandidate(candidate=candidate, source_archive=archive),
            ]
        ),
        private_store=private_store,
        clock=Clock(NOW + timedelta(minutes=1, seconds=30)),
    ).evaluate(request)
    first = private_store.get_evaluation(first_proposal.evidence_id)
    clean = private_store.get_evaluation(result.evidence.evidence_id)
    assert first is not None and clean is not None

    assert result.iterations == 2
    assert first.evidence.outcome == "redo"
    assert first.candidate_ref is None
    assert clean.evidence.outcome == "passed"
    assert clean.candidate_ref is not None
    assert first.evidence.evidence_id != clean.evidence.evidence_id
    assert first.evidence_ref != clean.evidence_ref
    assert evaluate_factory_release(job, _e2e_runs(job, 2), clean).status == "blocked"
    assert evaluate_factory_release(job, _e2e_runs(job, 3), clean).status == "ready"


@pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")
@pytest.mark.asyncio
async def test_real_mariadb_gateway_store_persists_the_captain_owned_chain(
    tmp_path: Path,
) -> None:
    assert TEST_DSN is not None
    assert_isolated_test_database(TEST_DSN)
    storage = MariaDBStorage(TEST_DSN)
    storage.clear()
    try:
        store = GatewayStore(storage)
        coordinator = FactoryCoordinator(GatewayFactoryRepository(store))
        bundle = await _evaluate_bundle(
            tmp_path,
            before_evaluate=lambda job, skill, lease: _record_evaluation_prerequisites(
                coordinator,
                store,
                job,
                skill,
                lease,
            ),
        )
        _record_evaluation_through_quality(coordinator, store, bundle)
        stored = store.factory_skill_evaluation(bundle.job.job_id)
        assert stored is not None
        release_submission = _release_submission(bundle, 3, stored)
        assert release_submission.decision.status == "ready"
        store.record_factory_release_decision(release_submission)
        store.publish_factory_skill(_publication(bundle))
        coordinator.record(_promotion(bundle))

        recovered = store.factory_job(bundle.job.job_id)
        assert recovered.projection.status is FactoryLifecycleStatus.READY_TO_USE
        assert recovered.projection.evaluation_id == bundle.submission.evidence.evidence_id
    finally:
        storage.clear()

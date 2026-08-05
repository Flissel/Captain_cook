from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agenten.agent_factory.candidate_evaluation import ResolvedFactoryCandidate
from agenten.agent_factory.capability_factory_production import (
    EvidenceLifecycleRequest,
    EvidenceRunRequest,
    EvidenceWorkflowReviewRequest,
)
from agenten.agent_factory.capability_live_adapters import ContentAddressedArtifactStore
from agenten.agent_factory.capability_v3_evidence_bridge import (
    CONTROLLED_RECOVERY_TODO_TOOL,
    CapabilityCandidateAttestationV1,
    CapabilityControlledHoldoutReceiptV1,
    CapabilityControlledRecoveryResultV1,
    CapabilityV3EvidenceBuilderContext,
    build_v3_job_from_package_c,
    build_capability_evidence_backend,
)
from agenten.agent_factory.contracts import (
    AgentFactoryJobV2,
    AgentFactoryJobV3,
    FactoryEvidenceBlock,
    FactoryRole,
)
from agenten.agent_factory.execution_budget import FactoryUsageReceiptV1
from agenten.agent_factory.execution_policy import FactoryExecutionPolicyV1
from agenten.agent_factory.factory_live_runner import (
    FactoryLiveEffectKind,
    FactoryLiveEffectReport,
    FactoryLiveRunReport,
)
from agenten.agent_factory.forge_contracts import CreationResultV1
from agenten.agent_factory.hermes_cli import ReleasedFactorySkillCatalog
from agenten.agent_factory.outcome_contracts import ForgeCapabilityPackageCandidateV1
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FACTORY_SKILL_ID_BY_STEP,
    FactoryFeedbackV1,
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_factory.team_execution import TeamExecutionCandidateAdapter
from agenten.agent_runtime.contracts import ArtifactRef


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def _ref(name: str, digest: str) -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://test/{name}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _job() -> AgentFactoryJobV2:
    return AgentFactoryJobV2.model_validate_json(
        Path("tests/fixtures/agent_factory/agent_factory_job.v2.json").read_text(
            encoding="utf-8"
        )
    )


def _result(job: AgentFactoryJobV2) -> CreationResultV1:
    fixture = CreationResultV1.model_validate_json(
        Path("tests/fixtures/contracts/minibook_creation_result.v1.json").read_text(
            encoding="utf-8"
        )
    )
    package = _addressed(fixture.package_manifest_ref)
    skill = _addressed(fixture.skill_usage_receipt_ref)
    return fixture.model_copy(
        update={
            "correlation_id": job.correlation_id,
            "subject_version": job.subject_version,
            "attempt": 1,
            "package_manifest_ref": package,
            "artifact_refs": tuple(_addressed(item) for item in fixture.artifact_refs),
            "evidence_refs": tuple(_addressed(item) for item in fixture.evidence_refs),
            "skill_usage_receipt_ref": skill,
        }
    )


def _candidate(job: AgentFactoryJobV2, result: CreationResultV1) -> ForgeCapabilityPackageCandidateV1:
    fixture = ForgeCapabilityPackageCandidateV1.model_validate_json(
        Path(
            "tests/fixtures/contracts/forge_capability_package_candidate.v1.json"
        ).read_text(encoding="utf-8")
    )
    addressed_artifacts = tuple(
        item.model_copy(update={"reference": _addressed(item.reference)})
        for item in fixture.artifacts
    )
    by_path = {item.path: item.reference for item in addressed_artifacts}
    return fixture.model_copy(
        update={
            "capability_id": job.required_capability,
            "factory_job_id": job.job_id,
            "creation_job_id": result.creation_job_id,
            "correlation_id": job.correlation_id,
            "subject_version": job.subject_version,
            "attempt": 1,
            "source_ref": _addressed(fixture.source_ref),
            "team_manifest_ref": by_path["team-manifest.json"],
            "artifacts": addressed_artifacts,
            "skill_usage_receipt_ref": _addressed(fixture.skill_usage_receipt_ref),
            "runbook_ref": by_path["RUNBOOK.md"],
        }
    )


def _addressed(reference: ArtifactRef | None) -> ArtifactRef | None:
    if reference is None:
        return None
    return reference.model_copy(
        update={"uri": f"artifact://test/content/{reference.sha256}"}
    )


def _policy() -> FactoryExecutionPolicyV1:
    return FactoryExecutionPolicyV1.model_validate(
        {
            "schema": "captain.factory-execution-policy.v1",
            "mode": "release",
            "live_execution": True,
            "max_cost_usd": "9.00",
            "max_runtime_seconds": 900,
            "required_live_runs": 3,
            "allowed_models": ["approved-model-id"],
            "live_capabilities": ["model.invoke"],
            "sandbox_mode": "workspace_write",
        }
    )


def test_v3_bridge_reuses_the_captain_registered_v3_job_identity() -> None:
    v2 = _job()
    v3 = AgentFactoryJobV3.model_validate(
        {
            **v2.model_dump(mode="json", by_alias=True),
            "schema": "captain.agent-factory-job.v3",
            "execution_policy": _policy().model_dump(mode="json", by_alias=True),
        }
    )

    assert build_v3_job_from_package_c(v3, _policy()) is v3


def test_runtime_evidence_request_preserves_registered_v3_job_on_json_round_trip() -> None:
    v2 = _job()
    v3 = AgentFactoryJobV3.model_validate(
        {
            **v2.model_dump(mode="json", by_alias=True),
            "schema": "captain.agent-factory-job.v3",
            "execution_policy": _policy().model_dump(mode="json", by_alias=True),
        }
    )
    result = _result(v2)
    candidate = _candidate(v2, result)
    request = EvidenceRunRequest(
        job=v3,
        creation_result=result,
        candidate=candidate,
        run_number=1,
    )

    restored = EvidenceRunRequest.model_validate_json(
        request.model_dump_json(by_alias=True)
    )

    assert isinstance(restored.job, AgentFactoryJobV3)
    assert restored.job.job_id == v3.job_id


class _Catalog(ReleasedFactorySkillCatalog):
    def released_for(self, job: object, step: FactorySkillStep) -> ReleasedHermesSkill:
        assert isinstance(job, AgentFactoryJobV3)
        skill_id = FACTORY_SKILL_ID_BY_STEP[step]
        digest = hashlib.sha256(skill_id.encode()).hexdigest()
        return ReleasedHermesSkill(
            schema_name="captain.released-hermes-skill.v1",
            skill_id=skill_id,
            version=1,
            capability=job.required_capability,
            content_ref=ArtifactRef(
                uri=f"artifact://released-skills/{skill_id}/v1",
                sha256=digest,
                media_type="application/json",
            ),
            content_sha256=digest,
            status="released",
            released_at=NOW,
            producer="captain",
        )


@dataclass
class _Authority:
    jobs: dict[UUID, AgentFactoryJobV3] = field(default_factory=dict)
    assignments: dict[tuple[UUID, FactorySkillStep], ReleasedHermesSkill] = field(
        default_factory=dict
    )
    leases: list[object] = field(default_factory=list)
    blocks: list[FactoryEvidenceBlock] = field(default_factory=list)
    usage: list[FactoryUsageReceiptV1] = field(default_factory=list)
    artifacts: list[TeamExecutionEvidenceV1 | TeamEvaluationV1 | FactoryFeedbackV1] = field(
        default_factory=list
    )

    def register(self, job: AgentFactoryJobV3) -> None:
        existing = self.jobs.get(job.job_id)
        if existing is not None and existing != job:
            raise ValueError("job changed")
        self.jobs[job.job_id] = job

    def job(self, job_id: UUID) -> AgentFactoryJobV3:
        return self.jobs[job_id]

    def seed_released_skill_assignments(
        self, job: AgentFactoryJobV3, source: ReleasedFactorySkillCatalog
    ) -> None:
        for step in FactorySkillStep:
            self.assignments[(job.job_id, step)] = source.released_for(job, step)

    def released_for(
        self, job: AgentFactoryJobV3, step: FactorySkillStep
    ) -> ReleasedHermesSkill:
        return self.assignments[(job.job_id, step)]

    def record_lease(self, lease: object) -> None:
        if lease not in self.leases:
            self.leases.append(lease)

    def record_block(self, block: FactoryEvidenceBlock) -> None:
        if block not in self.blocks:
            self.blocks.append(block)

    def usage_receipts(self, job_id: UUID) -> tuple[FactoryUsageReceiptV1, ...]:
        return tuple(item for item in self.usage if item.job_id == job_id)

    def workflow_artifacts(
        self, job_id: UUID
    ) -> tuple[TeamExecutionEvidenceV1 | TeamEvaluationV1 | FactoryFeedbackV1, ...]:
        return tuple(item for item in self.artifacts if item.job_id == job_id)

    async def persist_workflow_artifact(
        self,
        artifact: TeamExecutionEvidenceV1 | TeamEvaluationV1 | FactoryFeedbackV1,
    ) -> None:
        if artifact not in self.artifacts:
            self.artifacts.append(artifact)


@dataclass
class _CandidateProvider:
    resolved: ResolvedFactoryCandidate

    def candidate_for(
        self,
        job: AgentFactoryJobV3,
        candidate: ForgeCapabilityPackageCandidateV1,
    ) -> ResolvedFactoryCandidate:
        assert job.required_capability == candidate.capability_id
        return self.resolved


class _Attestation:
    async def attest(
        self,
        job: AgentFactoryJobV3,
        resolved: ResolvedFactoryCandidate,
        candidate: ForgeCapabilityPackageCandidateV1,
    ) -> CapabilityCandidateAttestationV1:
        assert resolved.candidate.source_archive_ref == candidate.source_ref
        return CapabilityCandidateAttestationV1(
            job_id=job.job_id,
            candidate_ref=candidate.source_ref,
            extracted_tree_sha256="e" * 64,
            sandbox_evidence_ref=_ref("sandbox", "e" * 64),
        )


def _invocation(job: AgentFactoryJobV3, dispatch: FactoryDispatch) -> FactorySkillInvocationV1:
    assert dispatch.lease is not None
    released = _Catalog().released_for(job, FactorySkillStep.EXECUTE_TEAM)
    binding = f"{job.job_id}|{dispatch.action.attempt}|execute-team"
    digest = hashlib.sha256(binding.encode()).hexdigest()
    return FactorySkillInvocationV1(
        schema_name="captain.factory-skill-invocation.v1",
        invocation_id=uuid5(NAMESPACE_URL, binding),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=dispatch.action.attempt,
        step=FactorySkillStep.EXECUTE_TEAM,
        released_skill=released,
        input_ref=job.input_ref,
        input_sha256=job.input_ref.sha256,
        lease=dispatch.lease,
        idempotency_key=digest,
        acceptance_assertion_ids=job.acceptance_assertion_ids,
        execution_scope_ref=job.private_holdout_refs[0],
    )


class _TeamService:
    def __init__(self, authority: _Authority, artifact_store: ContentAddressedArtifactStore):
        self._authority = authority
        self._artifacts = artifact_store

    async def execute(
        self,
        invocation: FactorySkillInvocationV1,
        _candidate: ResolvedFactoryCandidate,
        case_ref: object,
        *,
        run_number: int | None = None,
    ) -> TeamExecutionEvidenceV1:
        assert run_number is not None
        evidence_ref = self._artifacts.put(
            f'{{"run":{run_number},"provider":"live"}}'.encode(),
            "application/json",
            namespace="provider-run",
        )
        receipt = FactoryUsageReceiptV1(
            schema_name="captain.factory-usage-receipt.v1",
            receipt_id=uuid5(invocation.invocation_id, "usage"),
            reservation_id=uuid5(invocation.invocation_id, "reservation"),
            job_id=invocation.job_id,
            correlation_id=invocation.correlation_id,
            attempt=invocation.attempt,
            lease_id=invocation.lease.lease_id,
            invocation_id=invocation.invocation_id,
            provider="openai",
            model="approved-model-id",
            input_units=10,
            output_units=5,
            cost_usd=Decimal("0.10"),
            started_at=NOW + timedelta(seconds=run_number),
            ended_at=NOW + timedelta(seconds=run_number, milliseconds=1),
            evidence_ref=evidence_ref,
        )
        self._authority.usage.append(receipt)
        assertions = tuple(
            {
                "assertion_id": assertion_id,
                "status": "passed",
                "integration_intent": "none",
                "evidence_refs": (evidence_ref,),
            }
            for assertion_id in invocation.acceptance_assertion_ids
        )
        outcome = {
            "schema": "captain.execution-outcome.v1",
            "capability_id": "customer_support_triage",
            "capability_version": 1,
            "team_version": 1,
            "correlation_id": invocation.correlation_id,
            "command_id": uuid5(invocation.invocation_id, "command"),
            "result_id": uuid5(invocation.invocation_id, "result"),
            "output_ref": evidence_ref,
            "assertion_outcomes": assertions,
            "evidence_refs": (evidence_ref,),
            "status": "succeeded",
        }
        return TeamExecutionEvidenceV1(
            schema_name="hermes.factory-team-execution-evidence.v1",
            invocation=invocation,
            invocation_id=invocation.invocation_id,
            job_id=invocation.job_id,
            correlation_id=invocation.correlation_id,
            subject_version=invocation.subject_version,
            attempt=invocation.attempt,
            occurred_at=NOW + timedelta(seconds=run_number, milliseconds=2),
            producer="hermes",
            artifact_ref=evidence_ref,
            evidence_refs=(evidence_ref,),
            acceptance_assertion_ids=invocation.acceptance_assertion_ids,
            run_number=run_number,
            candidate_ref=_candidate.candidate.source_archive_ref,
            holdout_ref=case_ref,
            execution_outcome=outcome,
            usage_receipt_refs=(evidence_ref,),
            termination_reason="task_completed",
            status="succeeded",
        )


@dataclass
class _Recovery:
    team: TeamExecutionCandidateAdapter
    artifact_store: ContentAddressedArtifactStore

    async def execute(
        self,
        job: AgentFactoryJobV3,
        dispatch: FactoryDispatch,
        candidate: ResolvedFactoryCandidate,
    ) -> CapabilityControlledRecoveryResultV1:
        # Recovery has an independent invocation identity and therefore cannot be
        # mistaken for any of the three normal release runs.
        base = self.team.invocation_for(dispatch)
        recovery_invocation = base.model_copy(
            update={
                "invocation_id": uuid5(base.invocation_id, "controlled-recovery"),
                "idempotency_key": hashlib.sha256(
                    f"{base.idempotency_key}|controlled-recovery".encode()
                ).hexdigest(),
            }
        )
        service = _TeamService(AUTHORITY, self.artifact_store)
        execution = await service.execute(
            recovery_invocation,
            candidate,
            job.private_holdout_refs[0],
            run_number=1,
        )
        effect_id = uuid5(recovery_invocation.invocation_id, "effect")
        recovered_ref = execution.usage_receipt_refs[0]
        interrupted = FactoryLiveRunReport(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            mode="release",
            status="infrastructure_recovery_required",
            attempt=1,
            next_attempt=1,
            effects=(
                FactoryLiveEffectReport(
                    effect_id=effect_id,
                    kind=FactoryLiveEffectKind.PROVIDER,
                    attempt=1,
                    status="reserved",
                    reason="controlled post-effect interruption",
                    replayed=False,
                ),
            ),
            reasons=("controlled post-effect interruption",),
        )
        resumed = FactoryLiveRunReport(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            mode="release",
            status="ready",
            attempt=1,
            next_attempt=1,
            effects=(
                FactoryLiveEffectReport(
                    effect_id=effect_id,
                    kind=FactoryLiveEffectKind.PROVIDER,
                    attempt=1,
                    status="succeeded",
                    evidence_ref=recovered_ref,
                    completion_origin="recover",
                    replayed=False,
                ),
            ),
        )
        return CapabilityControlledRecoveryResultV1(
            recovery_id="controlled-recovery-01",
            recovery_assertion_id=job.acceptance_assertion_ids[0],
            execution=execution,
            interrupted=interrupted,
            resumed=resumed,
            provider_effect_receipt_ref=recovered_ref,
            holdout_receipts=(
                CapabilityControlledHoldoutReceiptV1(
                    holdout_ref=job.private_holdout_refs[0],
                    assertion_id=job.acceptance_assertion_ids[0],
                    status="passed",
                    evidence_ref=execution.artifact_ref,
                ),
            ),
        )


def _resolved_candidate(tmp_path: Path, candidate: ForgeCapabilityPackageCandidateV1) -> ResolvedFactoryCandidate:
    from agenten.agent_factory.candidate_evaluation import FactoryCandidateManifest
    from agenten.agent_factory.n8n_tools import TypedN8nTool

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"provider-backed-candidate")
    # The Package-C reference is authoritative for this focused bridge test.
    candidate = candidate.model_copy(update={"source_ref": candidate.source_ref})
    input_ref = _ref("input-schema", "8" * 64)
    output_ref = _ref("output-schema", "9" * 64)
    manifest = FactoryCandidateManifest(
        candidate_id="support_triage_v1",
        source_archive_ref=candidate.source_ref,
        team_manifest={"reference": candidate.team_manifest_ref, "relative_path": "team.json"},
        workflow_artifacts=(
            {"reference": _ref("workflow", "7" * 64), "relative_path": "workflow.json"},
        ),
        tool_schema_artifacts=(
            {"reference": input_ref, "relative_path": "input.json"},
            {"reference": output_ref, "relative_path": "output.json"},
        ),
        n8n_tools=(
            TypedN8nTool(
                name="support_triage",
                description="typed test tool",
                input_schema_ref=input_ref.uri,
                output_schema_ref=output_ref.uri,
            ),
        ),
        build_command=("python", "-m", "compileall"),
        real_case_command=("python", "run.py"),
        timeout_seconds=30,
    )
    return ResolvedFactoryCandidate(candidate=manifest, source_archive=source)


AUTHORITY = _Authority()


@pytest.mark.asyncio
async def test_bridge_persists_v3_authority_and_issues_recovery_then_three_runs(
    tmp_path: Path,
) -> None:
    global AUTHORITY
    AUTHORITY = _Authority()
    job = _job()
    result = _result(job)
    candidate = _candidate(job, result)
    artifacts = ContentAddressedArtifactStore(tmp_path / "artifacts")
    resolved = _resolved_candidate(tmp_path, candidate)

    def invocation_for(dispatch: FactoryDispatch) -> FactorySkillInvocationV1:
        assert isinstance(dispatch.job, AgentFactoryJobV3)
        return _invocation(dispatch.job, dispatch)

    team = TeamExecutionCandidateAdapter(
        service_for=lambda _job, _invocation: _TeamService(AUTHORITY, artifacts),
        invocation_for=invocation_for,
    )
    backend = build_capability_evidence_backend(
        context=CapabilityV3EvidenceBuilderContext(
            authority=AUTHORITY,
            released_skills=_Catalog(),
            candidate_provider=_CandidateProvider(resolved),
            team_execution=team,
            controlled_recovery=_Recovery(team, artifacts),
            candidate_attestation=_Attestation(),
            artifact_store=artifacts,
            execution_policy=_policy(),
            workspace_ref="workspace://captain/capability-live",
            clock=lambda: NOW + timedelta(seconds=30),
        )
    )

    receipts = tuple(
        [
            await backend.run(
                EvidenceRunRequest(
                    job=job,
                    creation_result=result,
                    candidate=candidate,
                    run_number=number,
                )
            )
            for number in range(1, 5)
        ]
    )
    accepted = tuple(item for item in receipts if item is not None)
    lifecycle = await backend.lifecycle_blocks(
        EvidenceLifecycleRequest(job=job, receipts=accepted)
    )

    assert len(AUTHORITY.jobs) == 1
    v3 = next(iter(AUTHORITY.jobs.values()))
    assert v3.schema_name == "captain.agent-factory-job.v3"
    assert v3.job_id != job.job_id
    assert v3.input_ref == job.input_ref
    assert v3.correlation_id == job.correlation_id
    assert set(step for current_job, step in AUTHORITY.assignments if current_job == v3.job_id) == set(FactorySkillStep)
    # The candidate attestation must become Captain's Build evidence before
    # the paid execute-team capability can obtain its tester lease.  Quality
    # remains deferred until the run evidence is complete.
    assert tuple(block.phase.value for block in AUTHORITY.blocks) == ("build_passed",)
    assert tuple(lease.role for lease in AUTHORITY.leases) == (
        FactoryRole.TOOL_INTEGRATOR,
        FactoryRole.REAL_CASE_TESTER,
    )
    assert tuple(item.record.kind for item in accepted) == (
        "recovery",
        "normal",
        "normal",
        "normal",
    )
    assert len({item.record.run_id for item in accepted}) == 4
    assert len({item.reference.sha256 for item in accepted}) == 4
    assertion_refs = tuple(
        reference
        for receipt in accepted
        for result in receipt.record.assertion_results
        for reference in result.evidence_refs
    )
    assert len(assertion_refs) == len({reference.sha256 for reference in assertion_refs})
    for reference in assertion_refs:
        payload = json.loads(artifacts.read_bytes(reference))
        assert payload["schema"] == "captain.capability-assertion-evidence.v1"
        assert payload["producer"] == "captain"
    assert len({item.invocation_id for item in AUTHORITY.usage}) == 4
    assert sum((item.cost_usd for item in AUTHORITY.usage), Decimal("0")) == Decimal("0.40")
    assert accepted[0].record.private_holdout_evidence[0].holdout_id == job.private_holdout_refs[0].holdout_id
    assert tuple(block.role for block in lifecycle) == (
        FactoryRole.TOOL_INTEGRATOR,
        FactoryRole.REAL_CASE_TESTER,
        FactoryRole.QUALITY_WARDEN,
    )
    assert all(block.lease_id is not None for block in lifecycle)

    await backend.workflow_review(
        EvidenceWorkflowReviewRequest(
            job=job,
            candidate=candidate,
            receipts=accepted,
        )
    )
    assert len(
        [item for item in AUTHORITY.artifacts if isinstance(item, TeamExecutionEvidenceV1)]
    ) == 3
    assert len(
        [item for item in AUTHORITY.artifacts if isinstance(item, TeamEvaluationV1)]
    ) == 1
    assert len(
        [item for item in AUTHORITY.artifacts if isinstance(item, FactoryFeedbackV1)]
    ) == 1

    replayed = await backend.run(
        EvidenceRunRequest(
            job=job,
            creation_result=result,
            candidate=candidate,
            run_number=4,
        )
    )
    assert replayed == accepted[-1]
    assert len(AUTHORITY.usage) == 4


def test_builder_fails_closed_without_controlled_recovery(tmp_path: Path) -> None:
    context = CapabilityV3EvidenceBuilderContext(
        authority=_Authority(),
        released_skills=_Catalog(),
        candidate_provider=object(),
        team_execution=object(),
        controlled_recovery=None,
        candidate_attestation=object(),
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        execution_policy=_policy(),
        workspace_ref="workspace://captain/capability-live",
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="TODO_TOOL.v1") as failure:
        build_capability_evidence_backend(context=context)
    assert CONTROLLED_RECOVERY_TODO_TOOL in str(failure.value)

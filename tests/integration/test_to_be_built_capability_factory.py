from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest

from agenten.agent_factory.capability_factory_entrypoint import (
    CapabilityFactoryConfigurationError,
    CapabilityFactoryDeadlineExceeded,
    CapabilityExecutionBundle,
    CapabilityExecutionPlan,
    CapabilityFactoryCheckpoint,
    CapabilityFactoryEntrypoint,
    CapabilityFactoryInputMutation,
    CapabilityFactoryRunSummary,
    CapabilityProviderIdempotencyError,
    CapabilityReleaseRunReceipt,
    CapabilityRuntimeExecution,
    CapabilitySandboxIsolationError,
    DockerCapabilitySandboxRunner,
    DockerCliCommandRunner,
    DockerCommandResult,
    FileCapabilityFactoryCheckpointStore,
    InMemoryCapabilityFactoryCheckpointStore,
    parse_capability_factory_args,
    run_capability_factory_cli,
    write_redacted_evidence_manifest,
)
from agenten.agent_factory.contracts import (
    AgentFactoryJobV2,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.forge_contracts import (
    CreationJobV1,
    CreationResultV1,
    CreationSubmissionReceipt,
    ReleasedSkillRefV1,
)
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.outcome_contracts import (
    AssertionOutcome,
    CapabilityAssertionResult,
    CapabilityReleaseEvidenceV1,
    ExecutionOutcomeV1,
    ForgeCapabilityPackageCandidateV1,
)
from agenten.agent_factory.outcome_validation import (
    CapabilitySandboxRequest,
    CapabilitySandboxResult,
    CapabilitySandboxTermination,
)
from agenten.agent_factory.release_gate import FactoryReleaseDecision
from agenten.agent_factory.service import FactoryCoordinator
from agenten.agent_factory.skill_evaluation import HermesSkillEvaluationEvidence
from agenten.agent_factory.skill_store import StoredSkillEvaluation
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    ProviderEffectReceipt,
)
from agenten.delivery.projector import MinibookProjector
from agenten.delivery.projection_cursor import ProjectionCursorStore
from gateway.capability_catalog import CapabilityCatalogRecord, GatewayCapabilityCatalog
from gateway.contracts import (
    CapabilityExecutionRecord,
    CapabilityExecutionRequest,
    CapabilityReleaseRequest,
    CapabilityWriteReceipt,
    FactoryReleaseDecisionSubmission,
    RuntimeExecutionClaim,
    RuntimeExecutionClaimReceipt,
    RuntimeResultRecoveryObservation,
    RuntimeResultRecoveryRequest,
    RuntimeOperationProjection,
    canonical_contract_sha256,
)
from gateway.factory_repository import GatewayFactoryRepository
from gateway.registry_feed import runtime_result_projection
from tests.agent_factory.test_skill_evaluation_contracts import evidence_payload


CORRELATION_ID = UUID("6b169405-f9f0-4d24-84bc-a7f098d2d8f1")
FACTORY_JOB_ID = UUID("1fcbdac4-31e1-48a2-b57b-f066896f09dd")
NOW = datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc)
FIXTURE_INPUT = (
    Path(__file__).parents[1]
    / "fixtures"
    / "agent_factory"
    / "TO_BE_BUILT.valid.md"
)


class SimulatedProcessCrash(RuntimeError):
    pass


@dataclass
class MutableClock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value


@dataclass
class MemoryContentStore:
    content_by_uri: dict[str, bytes] = field(default_factory=dict)

    def put(self, content: bytes, media_type: str, *, namespace: str = "factory") -> ArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        reference = ArtifactRef(
            uri=f"artifact://{namespace}/{digest}",
            sha256=digest,
            media_type=media_type,
        )
        self.content_by_uri[reference.uri] = content
        return reference

    async def read(self, reference: ArtifactRef) -> bytes:
        return self.content_by_uri[reference.uri]


@dataclass
class VerifiedScriptedSandboxRunner:
    requests: list[CapabilitySandboxRequest] = field(default_factory=list)
    clock: MutableClock | None = None
    advance_clock_after_validate: timedelta | None = None

    async def validate(self, request: CapabilitySandboxRequest) -> CapabilitySandboxResult:
        self.requests.append(request)
        if self.clock is not None and self.advance_clock_after_validate is not None:
            self.clock.value += self.advance_clock_after_validate
        return CapabilitySandboxResult(
            execution_id=request.execution_id,
            request_digest=request.request_digest,
            status="passed",
            imported_modules=request.module_names,
            executed_test_paths=request.test_paths,
            sandbox_identity="sandbox://scripted/verified-isolation",
            process_identity=request.process_identity,
            process_identity_verified=True,
            extracted_tree_sha256=request.extracted_tree_sha256,
            workspace_was_read_only=True,
            network_was_disabled=True,
            resource_limits_were_enforced=True,
            process_tree_termination_capable=True,
        )

    async def cancel(self, execution_id: UUID) -> None:
        return None

    async def await_termination(self, execution_id: UUID) -> CapabilitySandboxTermination:
        request = next(item for item in self.requests if item.execution_id == execution_id)
        return CapabilitySandboxTermination(
            execution_id=execution_id,
            request_digest=request.request_digest,
            sandbox_identity="sandbox://scripted/verified-isolation",
            process_identity=request.process_identity,
            process_identity_verified=True,
            extracted_tree_sha256=request.extracted_tree_sha256,
            terminated=True,
            process_tree_terminated=True,
        )


def _canonical_ref(model: object, name: str) -> ArtifactRef:
    content = json.dumps(
        model.model_dump(mode="json", by_alias=True),  # type: ignore[attr-defined]
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    return ArtifactRef(
        uri=f"artifact://gateway/{name}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return output.getvalue()


def test_checkpoint_binding_is_idempotent_and_rejects_changed_input_bytes() -> None:
    store = InMemoryCapabilityFactoryCheckpointStore()
    checkpoint = CapabilityFactoryCheckpoint(
        correlation_id=CORRELATION_ID,
        factory_job_id=FACTORY_JOB_ID,
        subject_version=1,
        input_sha256="a" * 64,
        occurred_at=NOW,
        deadline_at=NOW + timedelta(minutes=10),
    )

    assert store.bind(checkpoint) == checkpoint
    assert store.bind(checkpoint) == checkpoint

    with pytest.raises(CapabilityFactoryInputMutation, match="input bytes"):
        store.bind(checkpoint.model_copy(update={"input_sha256": "b" * 64}))


def test_file_checkpoint_binding_survives_process_restart(tmp_path: Path) -> None:
    checkpoint = CapabilityFactoryCheckpoint(
        correlation_id=CORRELATION_ID,
        factory_job_id=FACTORY_JOB_ID,
        subject_version=1,
        input_sha256="a" * 64,
        occurred_at=NOW,
        deadline_at=NOW + timedelta(minutes=10),
    )

    assert FileCapabilityFactoryCheckpointStore(tmp_path).bind(checkpoint) == checkpoint
    restarted = FileCapabilityFactoryCheckpointStore(tmp_path)
    assert restarted.bind(checkpoint) == checkpoint

    with pytest.raises(CapabilityFactoryInputMutation, match="input bytes"):
        restarted.bind(checkpoint.model_copy(update={"input_sha256": "b" * 64}))
    with pytest.raises(CapabilityFactoryInputMutation, match="timing"):
        restarted.bind(
            checkpoint.model_copy(
                update={"deadline_at": checkpoint.deadline_at + timedelta(seconds=1)}
            )
        )


def _accepted_evaluation(
    job: AgentFactoryJobV2,
    *,
    released_skill_ref: ArtifactRef,
    skill_evidence_ref: ArtifactRef,
) -> StoredSkillEvaluation:
    evidence = HermesSkillEvaluationEvidence.model_validate(evidence_payload())
    released_skill = evidence.request.released_skill.model_copy(
        update={
            "capability": job.required_capability,
            "content_ref": released_skill_ref,
            "content_sha256": released_skill_ref.sha256,
            "released_at": job.occurred_at,
        }
    )
    lease = evidence.request.lease.model_copy(
        update={
            "job_id": job.job_id,
            "correlation_id": job.correlation_id,
            "subject_version": job.subject_version,
            "issued_at": job.occurred_at,
            "expires_at": job.deadline_at,
        }
    )
    request = evidence.request.model_copy(
        update={
            "job_id": job.job_id,
            "correlation_id": job.correlation_id,
            "subject_version": job.subject_version,
            "acceptance_assertion_ids": job.acceptance_assertion_ids,
            "released_skill": released_skill,
            "lease": lease,
            "occurred_at": job.occurred_at,
        }
    )
    receipt = evidence.receipt.model_copy(
        update={
            "job_id": job.job_id,
            "correlation_id": job.correlation_id,
            "lease_id": lease.lease_id,
            "released_skill": released_skill,
            "used_skill_id": released_skill.skill_id,
            "used_skill_version": released_skill.version,
            "used_skill_sha256": released_skill.content_sha256,
            "assertion_ids": job.acceptance_assertion_ids,
            "occurred_at": job.occurred_at + timedelta(seconds=1),
            "evidence_refs": (skill_evidence_ref,),
        }
    )
    assert evidence.candidate is not None
    candidate = evidence.candidate.model_copy(
        update={
            "parent_released_skill": released_skill,
            "created_at": job.occurred_at + timedelta(seconds=2),
        }
    )
    checks = tuple(
        check.model_copy(
            update={
                "occurred_at": job.occurred_at + timedelta(seconds=3),
                "assertion_ids": (
                    job.acceptance_assertion_ids if check.kind == "test" else ()
                ),
            }
        )
        for check in evidence.checks
    )
    evidence = evidence.model_copy(
        update={
            "job_id": job.job_id,
            "correlation_id": job.correlation_id,
            "subject_version": job.subject_version,
            "occurred_at": job.occurred_at + timedelta(seconds=4),
            "request": request,
            "receipt": receipt,
            "candidate": candidate,
            "checks": checks,
            "assertion_ids": job.acceptance_assertion_ids,
            "outcome": "passed",
            "tool_gaps": (),
        }
    )
    return StoredSkillEvaluation(
        evidence=evidence,
        evidence_ref=_canonical_ref(evidence, "accepted-skill-evaluation"),
        receipt_ref=_canonical_ref(receipt, "accepted-skill-receipt"),
        tool_gaps=(),
        tool_gap_refs=(),
        candidate_ref=_canonical_ref(candidate, "retained-skill-candidate"),
    )


def _block(
    job: AgentFactoryJobV2,
    phase: FactoryPhase,
    *,
    occurred_at: datetime,
    assertions: tuple[str, ...] = (),
    evidence_refs: tuple[ArtifactRef, ...] = (),
) -> FactoryEvidenceBlock:
    role = {
        FactoryPhase.BLUEPRINT_CREATED: FactoryRole.AGENT_ARCHITECT,
        FactoryPhase.TOOL_CANDIDATE_TESTED: FactoryRole.TOOL_INTEGRATOR,
        FactoryPhase.AGENT_CODE_CREATED: FactoryRole.TOOL_INTEGRATOR,
        FactoryPhase.BUILD_PASSED: FactoryRole.TOOL_INTEGRATOR,
        FactoryPhase.REAL_CASE_EVIDENCE: FactoryRole.REAL_CASE_TESTER,
        FactoryPhase.QUALITY_REVIEWED: FactoryRole.QUALITY_WARDEN,
    }.get(phase)
    producer = "hermes" if role is not None else "captain"
    phase_ref = ArtifactRef(
        uri=f"artifact://factory-stage/{hashlib.sha256((str(job.job_id) + phase.value).encode()).hexdigest()}",
        sha256=hashlib.sha256((str(job.job_id) + phase.value).encode()).hexdigest(),
        media_type="application/json",
    )
    return FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id=uuid5(job.event_id, f"factory-stage:{phase.value}:1"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        causation_id=job.event_id,
        occurred_at=occurred_at,
        producer=producer,
        subject_version=job.subject_version,
        attempt=1,
        phase=phase,
        role=role,
        status=FactoryBlockStatus.SUCCEEDED,
        evidence_refs=evidence_refs or (phase_ref,),
        assertion_ids=assertions,
        lease_id=f"lease-{phase.value}-1" if role is not None else None,
    )


@dataclass
class DynamicPackage:
    job: AgentFactoryJobV2
    creation_job_id: UUID
    store: MemoryContentStore
    released_skill_ref: ArtifactRef
    required_gap: bool = False
    rejected_archive: bool = False
    failed_holdout: bool = False

    def __post_init__(self) -> None:
        files = {
            "team-manifest.json": json.dumps(
                {
                    "schema": "autogen-team.v1",
                    "capability_id": self.job.required_capability,
                    "capability_version": 1,
                    "autogen_modules": ["autogen/__init__.py", "autogen/team.py"],
                    "test_paths": ["tests/test_team.py"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            "autogen/__init__.py": b"from .team import CAPABILITY_ID\n",
            "autogen/team.py": (
                f"CAPABILITY_ID = {self.job.required_capability!r}\n"
            ).encode("utf-8"),
            "skills/autogen-agent-factory/SKILL.md": self.store.content_by_uri[
                self.released_skill_ref.uri
            ],
            "tests/test_team.py": (
                "from autogen.team import CAPABILITY_ID\n\n"
                f"def test_identity():\n    assert CAPABILITY_ID == {self.job.required_capability!r}\n"
            ).encode("utf-8"),
            "evidence/summary.json": b'{"status":"candidate"}\n',
            "RUNBOOK.md": b"# Capability runbook\n",
        }
        self.files = files
        archive = _zip_bytes(files)
        self.source_ref = self.store.put(archive, "application/zip", namespace="package")
        kind_by_path = {
            "team-manifest.json": "team_manifest",
            "autogen/__init__.py": "autogen_source",
            "autogen/team.py": "autogen_source",
            "skills/autogen-agent-factory/SKILL.md": "skill",
            "tests/test_team.py": "test",
            "evidence/summary.json": "evidence",
            "RUNBOOK.md": "runbook",
        }
        media_by_path = {
            "team-manifest.json": "application/json",
            "autogen/__init__.py": "text/x-python",
            "autogen/team.py": "text/x-python",
            "skills/autogen-agent-factory/SKILL.md": "text/markdown",
            "tests/test_team.py": "text/x-python",
            "evidence/summary.json": "application/json",
            "RUNBOOK.md": "text/markdown",
        }
        artifact_items = []
        refs_by_path: dict[str, ArtifactRef] = {}
        for path, content in files.items():
            reference = (
                self.released_skill_ref
                if path == "skills/autogen-agent-factory/SKILL.md"
                else self.store.put(content, media_by_path[path], namespace="package-file")
            )
            refs_by_path[path] = reference
            artifact_items.append(
                {"path": path, "kind": kind_by_path[path], "reference": reference}
            )
        self.artifact_refs = refs_by_path
        skill_evidence_ref = self.store.put(
            b'{"check":"released-skill-use","status":"passed"}',
            "application/json",
            namespace="skill-evidence",
        )
        evaluation = _accepted_evaluation(
            self.job,
            released_skill_ref=self.released_skill_ref,
            skill_evidence_ref=skill_evidence_ref,
        )
        self.evaluation = evaluation
        receipt = evaluation.evidence.receipt
        receipt_bytes = receipt.model_dump_json(by_alias=True).encode("utf-8")
        self.skill_receipt_ref = self.store.put(
            receipt_bytes,
            "application/json",
            namespace="skill-receipt",
        )
        tool_gaps: list[dict[str, object]] = []
        creation_gaps: list[dict[str, object]] = []
        if self.required_gap:
            input_ref = self.store.put(b'{"schema":"object"}', "application/json", namespace="gap")
            output_ref = self.store.put(b'{"schema":"object","type":"result"}', "application/json", namespace="gap")
            evidence_ref = self.store.put(b'{"status":"unresolved"}', "application/json", namespace="gap")
            gap = {
                "schema": "TODO_TOOL.v1",
                "gap_id": "missing-required-provider",
                "severity": "required",
                "input_contract_ref": input_ref,
                "output_contract_ref": output_ref,
                "least_privilege_capability": "provider.execute",
                "implementation_options": [
                    {
                        "option_id": "provide-approved-provider",
                        "description": "Configure the required approved provider.",
                        "acceptance_assertion_id": self.job.acceptance_assertion_ids[0],
                    }
                ],
                "acceptance_assertion_ids": [self.job.acceptance_assertion_ids[0]],
                "evidence_ref": evidence_ref,
                "status": "unresolved",
            }
            tool_gaps.append(gap)
            creation_gaps.append(
                {
                    "schema": "TODO_TOOL.v1",
                    "gap_id": gap["gap_id"],
                    "severity": gap["severity"],
                    "evidence_ref": evidence_ref.model_dump(mode="json"),
                    "status": "unresolved",
                }
            )

        self.candidate = ForgeCapabilityPackageCandidateV1.model_validate(
            {
                "schema": "forge.capability-package-candidate.v1",
                "capability_id": self.job.required_capability,
                "capability_version": 1,
                "factory_job_id": self.job.job_id,
                "creation_job_id": self.creation_job_id,
                "correlation_id": self.job.correlation_id,
                "subject_version": self.job.subject_version,
                "attempt": 1,
                "source_ref": self.source_ref,
                "team_manifest_ref": refs_by_path["team-manifest.json"],
                "artifacts": artifact_items,
                "skill_usage_receipt_ref": self.skill_receipt_ref,
                "tool_gaps": tool_gaps,
                "runbook_ref": refs_by_path["RUNBOOK.md"],
            }
        )
        candidate_bytes = self.candidate.model_dump_json(by_alias=True).encode("utf-8")
        self.candidate_ref = self.store.put(
            candidate_bytes,
            "application/json",
            namespace="candidate",
        )
        if self.rejected_archive:
            bad_bytes = self.store.content_by_uri[self.source_ref.uri] + b"tampered"
            self.store.content_by_uri[self.source_ref.uri] = bad_bytes
        self.creation_result = CreationResultV1.model_validate(
            {
                "schema": "minibook.creation-result.v1",
                "creation_job_id": self.creation_job_id,
                "correlation_id": self.job.correlation_id,
                "subject_version": self.job.subject_version,
                "attempt": 1,
                "status": "succeeded",
                "package_manifest_ref": self.candidate_ref.model_dump(mode="json"),
                "artifact_refs": [self.source_ref.model_dump(mode="json")],
                "evidence_refs": [],
                "tool_gaps": creation_gaps,
                "skill_usage_receipt_ref": self.skill_receipt_ref.model_dump(mode="json"),
            }
        )
        tree_entries = sorted(
            (path, hashlib.sha256(content).hexdigest(), len(content))
            for path, content in files.items()
        )
        self.tree_sha256 = hashlib.sha256(
            json.dumps(tree_entries, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def release_run(self, run_number: int) -> CapabilityReleaseRunReceipt:
        run_id = "controlled-recovery-01" if run_number == 1 else f"normal-e2e-{run_number - 1:02d}"
        assertion_results = []
        for assertion_id in self.job.acceptance_assertion_ids:
            evidence_bytes = json.dumps(
                {
                    "schema": "captain.capability-assertion-evidence.v1",
                    "run_id": run_id,
                    "assertion_id": assertion_id,
                    "status": "passed",
                    "producer": "captain",
                },
                separators=(",", ":"),
            ).encode("utf-8")
            evidence_ref = self.store.put(
                evidence_bytes,
                "application/json",
                namespace=f"assertion-run-{run_number}",
            )
            assertion_results.append(
                CapabilityAssertionResult(
                    assertion_id=assertion_id,
                    status="passed",
                    integration_intent="none",
                    evidence_refs=(evidence_ref,),
                )
            )
        holdouts = []
        if run_number == 1:
            for holdout in self.job.private_holdout_refs:
                holdout_ref = self.store.put(
                    json.dumps(
                        {"holdout_id": holdout.holdout_id, "status": "recorded"},
                        separators=(",", ":"),
                    ).encode("utf-8"),
                    "application/json",
                    namespace="holdout-evidence",
                )
                holdouts.append(
                    {
                        "holdout_id": holdout.holdout_id,
                        "assertion_id": self.job.acceptance_assertion_ids[0],
                        "status": "failed" if self.failed_holdout else "passed",
                        "evidence_ref": holdout_ref,
                    }
                )
        record = CapabilityReleaseEvidenceV1(
            schema_name="captain.capability-release-evidence.v1",
            run_id=run_id,
            run_number=run_number,
            factory_job_id=self.job.job_id,
            creation_job_id=self.creation_job_id,
            correlation_id=self.job.correlation_id,
            subject_version=self.job.subject_version,
            attempt=1,
            capability_id=self.job.required_capability,
            capability_version=1,
            candidate_manifest_sha256=self.candidate_ref.sha256,
            package_archive_sha256=self.source_ref.sha256,
            extracted_tree_sha256=self.tree_sha256,
            kind="recovery" if run_number == 1 else "normal",
            outcome="expected_failure_recovered" if run_number == 1 else "succeeded",
            producer="captain",
            assertion_results=tuple(assertion_results),
            recovery_id="controlled-recovery-01" if run_number == 1 else None,
            recovery_assertion_id=(
                self.job.acceptance_assertion_ids[0] if run_number == 1 else None
            ),
            private_holdout_evidence=tuple(holdouts),
        )
        content = record.model_dump_json(by_alias=True).encode("utf-8")
        reference = self.store.put(
            content,
            "application/json",
            namespace="release-evidence",
        )
        return CapabilityReleaseRunReceipt(record=record, reference=reference)


@dataclass
class ScriptedGatewayStore:
    clock: MutableClock
    jobs: dict[UUID, AgentFactoryJobV2] = field(default_factory=dict)
    blocks_by_job: dict[UUID, dict[UUID, FactoryEvidenceBlock]] = field(default_factory=dict)
    evaluations: dict[UUID, StoredSkillEvaluation] = field(default_factory=dict)
    release_decisions: dict[UUID, FactoryReleaseDecision] = field(default_factory=dict)
    terminal_decisions: dict[UUID, object] = field(default_factory=dict)
    catalog_records: dict[str, CapabilityCatalogRecord] = field(default_factory=dict)
    releases: dict[UUID, CapabilityReleaseRequest] = field(default_factory=dict)
    runtime_commands: dict[UUID, AgentRuntimeCommand] = field(default_factory=dict)
    runtime_grants: dict[UUID, CapabilityGrant] = field(default_factory=dict)
    runtime_claims: dict[UUID, RuntimeExecutionClaim] = field(default_factory=dict)
    runtime_claim_history: dict[tuple[UUID, int], RuntimeExecutionClaim] = field(
        default_factory=dict
    )
    runtime_results: dict[UUID, AgentRuntimeResult] = field(default_factory=dict)
    runtime_recoveries: dict[UUID, RuntimeResultRecoveryObservation] = field(
        default_factory=dict
    )
    executions: dict[UUID, CapabilityExecutionRecord] = field(default_factory=dict)
    projection_events_by_correlation: dict[UUID, list[object]] = field(default_factory=dict)
    release_effects: list[UUID] = field(default_factory=list)
    execution_effects: list[UUID] = field(default_factory=list)
    authority_order: list[str] = field(default_factory=list)
    runtime_authority_order: list[str] = field(default_factory=list)
    crash_after_publication_once: bool = False
    crash_after_execution_once: bool = False
    crash_before_job_registration_once: bool = False
    crash_after_claim_once: bool = False
    crash_after_result_once: bool = False
    result_committed_before_reclaim_once: AgentRuntimeResult | None = None
    add_decoy_projection: bool = False
    _crashed_after_publication: bool = False
    _crashed_after_execution: bool = False
    _crashed_before_job_registration: bool = False
    _crashed_after_claim: bool = False
    _crashed_after_result: bool = False
    _committed_result_before_reclaim: bool = False
    advance_clock_after_publication: timedelta | None = None
    advance_clock_after_execution: timedelta | None = None

    def record_factory_job(self, job: AgentFactoryJobV2) -> object:
        if (
            self.crash_before_job_registration_once
            and not self._crashed_before_job_registration
        ):
            self._crashed_before_job_registration = True
            raise SimulatedProcessCrash("before Gateway job registration")
        existing = self.jobs.get(job.job_id)
        if existing is not None and existing != job:
            raise AssertionError("changed factory job replay")
        self.jobs[job.job_id] = job
        return SimpleNamespace(replayed=existing is not None)

    def factory_job(self, job_id: UUID) -> object:
        return SimpleNamespace(
            job=self.jobs[job_id],
            blocks=tuple(self.blocks_by_job.get(job_id, {}).values()),
        )

    def record_factory_block(self, block: FactoryEvidenceBlock) -> object:
        records = self.blocks_by_job.setdefault(block.job_id, {})
        existing = records.get(block.event_id)
        if existing is not None and existing != block:
            raise AssertionError("changed factory block replay")
        records[block.event_id] = block
        if block.phase is FactoryPhase.CAPABILITY_PROMOTED and existing is None:
            self.authority_order.append("promotion")
        return SimpleNamespace(replayed=existing is not None)

    def factory_skill_evaluation(self, job_id: UUID) -> StoredSkillEvaluation | None:
        return self.evaluations.get(job_id)

    def factory_release_decision(self, job_id: UUID) -> FactoryReleaseDecision | None:
        return self.release_decisions.get(job_id)

    def record_factory_release_decision(
        self,
        submission: FactoryReleaseDecisionSubmission,
    ) -> object:
        decision = submission.decision
        existing = self.release_decisions.get(decision.job_id)
        if existing is not None and existing != decision:
            raise AssertionError("changed release decision replay")
        self.release_decisions[decision.job_id] = decision
        return SimpleNamespace(replayed=existing is not None)

    def record_factory_terminal_decision(self, decision: object) -> object:
        assert decision.state != "ready_to_use"  # type: ignore[attr-defined]
        job_id = decision.job_id  # type: ignore[attr-defined]
        existing = self.terminal_decisions.get(job_id)
        if existing is not None and existing != decision:
            raise AssertionError("changed terminal decision replay")
        self.terminal_decisions[job_id] = decision
        return SimpleNamespace(replayed=existing is not None)

    def factory_terminal_decision(self, job_id: UUID) -> object | None:
        return self.terminal_decisions.get(job_id)

    def find_ready_capability(self, capability_id: str) -> CapabilityCatalogRecord | None:
        return self.catalog_records.get(capability_id)

    def publish_capability_release(
        self,
        request: CapabilityReleaseRequest,
    ) -> CapabilityWriteReceipt:
        existing = self.releases.get(request.package.factory_job_id)
        if existing is not None:
            if existing != request:
                raise AssertionError("changed capability release replay")
            return CapabilityWriteReceipt(
                record_id=f"{request.package.capability_id}:{request.package.capability_version}",
                replayed=True,
            )
        self.releases[request.package.factory_job_id] = request
        self.release_effects.append(request.event_id)
        assert request.package.factory_job_id not in self.terminal_decisions
        assert self.authority_order[-1] == "promotion"
        self.terminal_decisions[request.package.factory_job_id] = request.decision
        self.catalog_records[request.package.capability_id] = CapabilityCatalogRecord.from_release(
            request,
            catalog_fence=1,
        )
        self.authority_order.append("atomic-publication")
        if self.advance_clock_after_publication is not None:
            self.clock.value += self.advance_clock_after_publication
        if self.crash_after_publication_once and not self._crashed_after_publication:
            self._crashed_after_publication = True
            raise SimulatedProcessCrash("after atomic publication")
        return CapabilityWriteReceipt(
            record_id=f"{request.package.capability_id}:{request.package.capability_version}",
            replayed=False,
        )

    def capability(
        self,
        capability_id: str,
        *,
        version: int | None = None,
    ) -> CapabilityCatalogRecord | None:
        record = self.catalog_records.get(capability_id)
        if record is None or (version is not None and record.capability_version != version):
            return None
        return record

    def accept_runtime_command(self, command: AgentRuntimeCommand) -> object:
        existing = self.runtime_commands.get(command.event_id)
        if existing is not None and existing != command:
            raise AssertionError("changed runtime command replay")
        self.runtime_commands[command.event_id] = command
        if existing is None:
            self.runtime_authority_order.append("command")
        return SimpleNamespace(replayed=existing is not None)

    def record_capability_grant(self, grant: CapabilityGrant) -> object:
        existing = self.runtime_grants.get(grant.command_id)
        if existing is not None and existing != grant:
            raise AssertionError("changed runtime grant replay")
        self.runtime_grants[grant.command_id] = grant
        if existing is None:
            self.runtime_authority_order.append("grant")
        return SimpleNamespace(replayed=existing is not None)

    def claim_runtime_execution(self, request: object) -> RuntimeExecutionClaimReceipt:
        command_id = request.command_id  # type: ignore[attr-defined]
        existing = self.runtime_claims.get(command_id)
        if (
            existing is not None
            and existing.expires_at <= self.clock.now()
            and self.result_committed_before_reclaim_once is not None
            and not self._committed_result_before_reclaim
        ):
            result = self.result_committed_before_reclaim_once
            assert result.command_id == command_id
            assert existing.claimed_at <= result.occurred_at < existing.expires_at
            self.runtime_results[command_id] = result
            self.runtime_claims[command_id] = existing.model_copy(
                update={"status": "completed", "completed_at": result.occurred_at}
            )
            self.runtime_authority_order.append("result")
            self._committed_result_before_reclaim = True
            raise RuntimeError("runtime execution is already completed")
        if existing is not None and existing.status == "active" and existing.expires_at > self.clock.now():
            return RuntimeExecutionClaimReceipt(
                claim=existing,
                replayed=True,
                recovered=False,
                claim_credential=None,
            )
        fencing_token = existing.fencing_token + 1 if existing is not None else 1
        authority = self.catalog_records[request.capability_id]  # type: ignore[attr-defined]
        claim = RuntimeExecutionClaim(
            capability_id=authority.capability_id,
            capability_version=authority.capability_version,
            team_version=authority.team_version,
            catalog_fence=authority.catalog_fence,
            catalog_block_index=101,
            catalog_block_hash="c" * 64,
            package_block_index=100,
            package_block_hash="b" * 64,
            package_ref=authority.package_ref,
            published_at=authority.published_at,
            command_id=command_id,
            claim_id=uuid5(command_id, f"runtime-execution-claim:{fencing_token}"),
            owner_id=request.owner_id,  # type: ignore[attr-defined]
            fencing_token=fencing_token,
            claimed_at=self.clock.now(),
            expires_at=self.clock.now() + timedelta(seconds=request.lease_seconds),  # type: ignore[attr-defined]
            status="active",
        )
        self.runtime_claims[command_id] = claim
        self.runtime_claim_history[(command_id, fencing_token)] = claim
        self.runtime_authority_order.append("claim")
        if self.crash_after_claim_once and not self._crashed_after_claim:
            self._crashed_after_claim = True
            raise SimulatedProcessCrash("after runtime claim")
        return RuntimeExecutionClaimReceipt(
            claim=claim,
            replayed=False,
            recovered=existing is not None,
            claim_credential="deterministic-runtime-claim-credential",
        )

    def record_runtime_result(
        self,
        result: AgentRuntimeResult,
        *,
        execution_owner_id: str,
        execution_fencing_token: int,
        execution_claim_credential: str,
    ) -> object:
        claim = self.runtime_claims[result.command_id]
        assert execution_owner_id == claim.owner_id
        assert execution_fencing_token == claim.fencing_token
        assert execution_claim_credential == "deterministic-runtime-claim-credential"
        assert claim.claimed_at <= result.occurred_at < claim.expires_at
        existing = self.runtime_results.get(result.command_id)
        if existing is not None and existing != result:
            raise AssertionError("changed runtime result replay")
        self.runtime_results[result.command_id] = result
        self.runtime_claims[result.command_id] = claim.model_copy(
            update={"status": "completed", "completed_at": result.occurred_at}
        )
        if existing is None:
            self.runtime_authority_order.append("result")
        if self.crash_after_result_once and not self._crashed_after_result:
            self._crashed_after_result = True
            raise SimulatedProcessCrash("after runtime result")
        return SimpleNamespace(replayed=existing is not None)

    def recover_runtime_result(
        self,
        request: RuntimeResultRecoveryRequest,
        *,
        execution_owner_id: str,
        execution_fencing_token: int,
        execution_claim_credential: str,
    ) -> object:
        observation = request.observation
        claim = self.runtime_claims[observation.command_id]
        original_claim = self.runtime_claim_history[
            (observation.command_id, observation.original_claim_fence)
        ]
        assert execution_owner_id == claim.owner_id
        assert execution_fencing_token == claim.fencing_token
        assert execution_claim_credential == "deterministic-runtime-claim-credential"
        assert observation.recovery_claim_fence == claim.fencing_token
        assert observation.original_claim_id == original_claim.claim_id
        assert observation.original_claim_digest == canonical_contract_sha256(
            original_claim
        )
        assert request.provider_receipt.origin_claim_id == original_claim.claim_id
        assert (
            request.provider_receipt.origin_claim_fencing_token
            == original_claim.fencing_token
        )
        assert request.provider_receipt.origin_claim_digest == canonical_contract_sha256(
            original_claim
        )
        assert claim.claimed_at <= observation.observed_at < claim.expires_at
        assert (
            original_claim.claimed_at
            <= request.result.occurred_at
            < original_claim.expires_at
        )
        existing_result = self.runtime_results.get(observation.command_id)
        existing_observation = self.runtime_recoveries.get(observation.command_id)
        if existing_result is not None or existing_observation is not None:
            if existing_result != request.result or existing_observation != observation:
                raise AssertionError("changed runtime result recovery replay")
            return SimpleNamespace(replayed=True)
        self.runtime_results[observation.command_id] = request.result
        self.runtime_recoveries[observation.command_id] = observation
        self.runtime_claims[observation.command_id] = claim.model_copy(
            update={"status": "completed", "completed_at": observation.observed_at}
        )
        self.runtime_authority_order.extend(("result", "result-recovery"))
        return SimpleNamespace(replayed=False)

    def runtime_operation(self, operation_id: UUID) -> RuntimeOperationProjection:
        return RuntimeOperationProjection(
            operation_id=operation_id,
            command=self.runtime_commands[operation_id],
            grant=self.runtime_grants.get(operation_id),
            result=self.runtime_results.get(operation_id),
        )

    def find_runtime_operation(
        self,
        operation_id: UUID,
    ) -> RuntimeOperationProjection | None:
        if operation_id not in self.runtime_commands:
            return None
        return self.runtime_operation(operation_id)

    def runtime_execution_claim(self, operation_id: UUID) -> RuntimeExecutionClaim | None:
        return self.runtime_claims.get(operation_id)

    def runtime_result_recovery(
        self,
        operation_id: UUID,
    ) -> RuntimeResultRecoveryObservation | None:
        return self.runtime_recoveries.get(operation_id)

    def record_capability_execution(
        self,
        request: CapabilityExecutionRequest,
    ) -> CapabilityWriteReceipt:
        existing = self.executions.get(request.outcome.command_id)
        if existing is not None:
            if existing.outcome != request.outcome:
                raise AssertionError("changed execution replay")
            return CapabilityWriteReceipt(
                record_id=str(request.outcome.command_id),
                replayed=True,
            )
        record = CapabilityExecutionRecord.from_request(request, catalog_fence=1)
        self.executions[request.outcome.command_id] = record
        self.execution_effects.append(request.event_id)
        projection = runtime_result_projection(
            self.runtime_results[request.outcome.command_id].model_dump(
                mode="json", by_alias=True
            )
        )
        assert projection is not None
        projection_feed = self.projection_events_by_correlation.setdefault(
            request.outcome.correlation_id,
            [],
        )
        if self.add_decoy_projection:
            projection_feed.append(
                projection.model_copy(
                    update={
                        "event_id": uuid5(projection.event_id, "decoy-result"),
                    }
                )
            )
        projection_feed.append(projection)
        self.runtime_authority_order.append("execution")
        if self.advance_clock_after_execution is not None:
            self.clock.value += self.advance_clock_after_execution
        if self.crash_after_execution_once and not self._crashed_after_execution:
            self._crashed_after_execution = True
            raise SimulatedProcessCrash("after capability execution")
        return CapabilityWriteReceipt(
            record_id=str(request.outcome.command_id),
            replayed=False,
        )

    def projection_events(self, correlation_id: UUID) -> tuple[object, ...]:
        return tuple(self.projection_events_by_correlation.get(correlation_id, ()))

    def capability_execution(self, command_id: UUID) -> CapabilityExecutionRecord | None:
        return self.executions.get(command_id)


@dataclass
class ScriptedCreationPort:
    store: MemoryContentStore
    gateway: ScriptedGatewayStore
    released_skill_ref: ArtifactRef
    crash_after_submit_once: bool = False
    required_gap: bool = False
    rejected_archive: bool = False
    failed_holdout: bool = False
    advance_clock_after_result: timedelta | None = None
    prepared: dict[UUID, DynamicPackage] = field(default_factory=dict)
    submission_effects: list[UUID] = field(default_factory=list)
    submitted_jobs: dict[UUID, CreationJobV1] = field(default_factory=dict)
    _crashed_after_submit: bool = False

    async def preparation_blocks(
        self,
        job: AgentFactoryJobV2,
        creation_job: CreationJobV1,
    ) -> tuple[FactoryEvidenceBlock, FactoryEvidenceBlock]:
        package = self.prepared.get(creation_job.creation_job_id)
        if package is None:
            package = DynamicPackage(
                job=job,
                creation_job_id=creation_job.creation_job_id,
                store=self.store,
                released_skill_ref=self.released_skill_ref,
                required_gap=self.required_gap,
                rejected_archive=self.rejected_archive,
                failed_holdout=self.failed_holdout,
            )
            self.prepared[creation_job.creation_job_id] = package
            self.gateway.evaluations[job.job_id] = package.evaluation
        return (
            _block(
                job,
                FactoryPhase.BLUEPRINT_CREATED,
                occurred_at=job.occurred_at + timedelta(seconds=5),
            ),
            _block(
                job,
                FactoryPhase.TOOL_CANDIDATE_TESTED,
                occurred_at=job.occurred_at + timedelta(seconds=6),
            ),
        )

    async def submit(self, creation_job: CreationJobV1) -> CreationSubmissionReceipt:
        existing = self.submitted_jobs.get(creation_job.creation_job_id)
        if existing is not None and existing != creation_job:
            raise AssertionError("changed CreationJob replay")
        if existing is None:
            self.submitted_jobs[creation_job.creation_job_id] = creation_job
            self.submission_effects.append(creation_job.creation_job_id)
            if self.crash_after_submit_once and not self._crashed_after_submit:
                self._crashed_after_submit = True
                raise SimulatedProcessCrash("after creation submission")
        return CreationSubmissionReceipt(
            creation_job_id=creation_job.creation_job_id,
            status="queued",
            subject_version=creation_job.subject_version,
            replayed=existing is not None,
        )

    async def result(self, creation_job_id: UUID) -> CreationResultV1:
        result = self.prepared[creation_job_id].creation_result
        if self.advance_clock_after_result is not None:
            self.gateway.clock.value += self.advance_clock_after_result
        return result

    async def completion_block(
        self,
        job: AgentFactoryJobV2,
        result: CreationResultV1,
    ) -> FactoryEvidenceBlock:
        return _block(
            job,
            FactoryPhase.AGENT_CODE_CREATED,
            occurred_at=job.occurred_at + timedelta(seconds=7),
            evidence_refs=(
                ArtifactRef.model_validate(
                    result.package_manifest_ref.model_dump(mode="json")
                ),
            ),
        )


@dataclass
class ScriptedReleaseRunPort:
    creation: ScriptedCreationPort
    crash_after_run_once: int | None = None
    max_success_runs: int = 3
    effects: list[str] = field(default_factory=list)
    lifecycle_calls: int = 0
    receipts: dict[tuple[UUID, int], CapabilityReleaseRunReceipt] = field(default_factory=dict)
    _crashed: bool = False

    async def run(
        self,
        job: AgentFactoryJobV2,
        creation_result: CreationResultV1,
        candidate: ForgeCapabilityPackageCandidateV1,
        run_number: int,
    ) -> CapabilityReleaseRunReceipt | None:
        if run_number > self.max_success_runs + 1:
            return None
        key = (job.job_id, run_number)
        existing = self.receipts.get(key)
        if existing is None:
            package = self.creation.prepared[creation_result.creation_job_id]
            existing = package.release_run(run_number)
            self.receipts[key] = existing
            self.effects.append(existing.record.run_id)
            if self.crash_after_run_once == run_number and not self._crashed:
                self._crashed = True
                raise SimulatedProcessCrash(f"after release run {run_number}")
        return existing

    async def lifecycle_blocks(
        self,
        job: AgentFactoryJobV2,
        receipts: tuple[CapabilityReleaseRunReceipt, ...],
    ) -> tuple[FactoryEvidenceBlock, FactoryEvidenceBlock, FactoryEvidenceBlock]:
        self.lifecycle_calls += 1
        refs = tuple(item.reference for item in receipts)
        return (
            _block(
                job,
                FactoryPhase.BUILD_PASSED,
                occurred_at=job.occurred_at + timedelta(seconds=8),
                evidence_refs=refs,
            ),
            _block(
                job,
                FactoryPhase.REAL_CASE_EVIDENCE,
                occurred_at=job.occurred_at + timedelta(seconds=9),
                assertions=job.acceptance_assertion_ids,
                evidence_refs=refs,
            ),
            _block(
                job,
                FactoryPhase.QUALITY_REVIEWED,
                occurred_at=job.occurred_at + timedelta(seconds=10),
                assertions=job.acceptance_assertion_ids,
                evidence_refs=refs,
            ),
        )


@dataclass
class ScriptedRuntimePort:
    gateway: ScriptedGatewayStore
    effects: list[UUID] = field(default_factory=list)
    plans: dict[UUID, CapabilityExecutionPlan] = field(default_factory=dict)
    executions: dict[UUID, CapabilityRuntimeExecution] = field(default_factory=dict)
    advance_clock_after_execute: timedelta | None = None
    durable_idempotency_guaranteed: bool = True
    crash_after_provider_effect_once: bool = False
    _crashed_after_provider_effect: bool = False

    def guarantees_durable_idempotency(
        self,
        plan: CapabilityExecutionPlan,
        authority: CapabilityCatalogRecord,
    ) -> bool:
        return self.durable_idempotency_guaranteed

    async def lookup_effect(
        self,
        *,
        command_id: UUID,
        effect_id: UUID,
    ) -> CapabilityRuntimeExecution | None:
        execution = self.executions.get(effect_id)
        if (
            execution is not None
            and execution.provider_receipt.command_id != command_id
        ):
            raise AssertionError("provider command identity changed")
        return execution

    async def prepare(
        self,
        job: AgentFactoryJobV2,
        authority: CapabilityCatalogRecord,
    ) -> CapabilityExecutionPlan:
        command_id = uuid5(job.correlation_id, "capability-execution-command")
        existing = self.plans.get(command_id)
        if existing is not None:
            return existing
        prompt_ref = ArtifactRef(
            uri=f"artifact://runtime-prompt/{'a' * 64}",
            sha256="a" * 64,
            media_type="text/markdown",
        )
        command = AgentRuntimeCommand.model_validate(
            {
                "schema": "captain.agent-runtime-command.v1",
                "event_id": command_id,
                "correlation_id": job.correlation_id,
                "causation_id": authority.terminal_decision_id,
                "occurred_at": job.occurred_at,
                "producer": "captain",
                "subject_id": "capability-execution",
                "subject_version": job.subject_version,
                "payload": {
                    "operation": "codex.run",
                    "project_id": "capability-factory",
                    "batch_id": "capability-release",
                    "subtask_id": "capability-execution",
                    "workspace_ref": "workspace://capability-factory/execution",
                    "prompt_ref": prompt_ref,
                    "integration_intent": "none",
                    "capability_profile": "code-builder",
                    "limits": {"wall_seconds": 60, "max_iterations": 1},
                },
            }
        )
        grant = CapabilityGrant.model_validate(
            {
                "schema": "captain.capability-grant.v1",
                "grant_id": "capability-release-grant",
                "command_id": command.event_id,
                "batch_id": command.payload.batch_id,
                "batch_version": command.subject_version,
                "subtask_id": command.payload.subtask_id,
                "workspace_ref": command.payload.workspace_ref,
                "profile": command.payload.capability_profile,
                "capabilities": ["codex.run"],
                "mcp_servers": [],
                "issued_at": job.occurred_at,
                "expires_at": min(job.deadline_at, job.occurred_at + timedelta(minutes=5)),
            }
        )
        plan = CapabilityExecutionPlan(
            command=command,
            grant=grant,
            claim_owner_id="capability-factory-runtime",
        )
        self.plans[command_id] = plan
        return plan

    async def execute(
        self,
        plan: CapabilityExecutionPlan,
        authority: CapabilityCatalogRecord,
        claim: RuntimeExecutionClaimReceipt,
        *,
        effect_id: UUID,
    ) -> CapabilityRuntimeExecution:
        command = plan.command
        command_id = command.event_id
        existing = self.executions.get(effect_id)
        if existing is not None:
            return existing
        assert claim.claim.command_id == command_id
        assert claim.claim.capability_id == authority.capability_id
        assert claim.claim.capability_version == authority.capability_version
        assert claim.claim_credential is not None
        assert effect_id == uuid5(command_id, "durable-provider-effect")
        result_id = uuid5(command_id, "capability-execution-result")
        artifact_ref = ArtifactRef(
            uri=f"artifact://runtime-result/{'b' * 64}",
            sha256="b" * 64,
            media_type="application/json",
        )
        result = AgentRuntimeResult.model_validate(
            {
                "schema": "captain.agent-runtime-result.v1",
                "event_id": result_id,
                "command_id": command_id,
                "correlation_id": command.correlation_id,
                "occurred_at": self.gateway.clock.now() + timedelta(seconds=1),
                "producer": "agent-runtime",
                "subject_id": command.subject_id,
                "subject_version": command.subject_version,
                "grant_id": plan.grant.grant_id,
                "operation": "codex.run",
                "status": "succeeded",
                "session_id": "capability-release-session",
                "artifact_refs": [artifact_ref],
                "evidence_refs": [artifact_ref],
                "error": None,
            }
        )
        outcome = await self.derive_outcome(plan, authority, result)
        execution = CapabilityRuntimeExecution(
            result=result,
            outcome=outcome,
            provider_receipt=ProviderEffectReceipt(
                provider_operation_id=(
                    "provider-operation:"
                    + str(effect_id)
                ),
                effect_id=effect_id,
                command_id=command_id,
                origin_claim_id=claim.claim.claim_id,
                origin_claim_fencing_token=claim.claim.fencing_token,
                origin_claim_digest=canonical_contract_sha256(claim.claim),
                request_digest=canonical_contract_sha256(plan),
                result_digest=canonical_contract_sha256(result),
                status=result.status.value,
                idempotency_guaranteed=self.durable_idempotency_guaranteed,
            ),
        )
        self.executions[effect_id] = execution
        self.effects.append(command_id)
        if (
            self.crash_after_provider_effect_once
            and not self._crashed_after_provider_effect
        ):
            self._crashed_after_provider_effect = True
            raise SimulatedProcessCrash("after durable provider effect")
        if self.advance_clock_after_execute is not None:
            self.gateway.clock.value += self.advance_clock_after_execute
        return execution

    async def derive_outcome(
        self,
        plan: CapabilityExecutionPlan,
        authority: CapabilityCatalogRecord,
        result: AgentRuntimeResult,
    ) -> ExecutionOutcomeV1:
        command = plan.command
        if (
            result.command_id != command.event_id
            or result.correlation_id != command.correlation_id
            or not result.artifact_refs
        ):
            raise AssertionError("runtime result does not match the deterministic plan")
        artifact_ref = result.artifact_refs[0]
        assertion_outcomes = tuple(
            AssertionOutcome(
                assertion_id=assertion_id,
                status="passed",
                integration_intent="none",
                evidence_refs=(
                    ArtifactRef(
                        uri=f"artifact://execution/{hashlib.sha256(assertion_id.encode()).hexdigest()}",
                        sha256=hashlib.sha256(assertion_id.encode()).hexdigest(),
                        media_type="application/json",
                    ),
                ),
            )
            for assertion_id in authority.accepted_assertion_ids
        )
        outcome = ExecutionOutcomeV1(
            schema_name="captain.execution-outcome.v1",
            capability_id=authority.capability_id,
            capability_version=authority.capability_version,
            team_version=authority.team_version,
            correlation_id=command.correlation_id,
            command_id=command.event_id,
            result_id=result.event_id,
            business_output={"status": "completed"},
            assertion_outcomes=assertion_outcomes,
            tool_versions=("captain.runtime@1",),
            workflow_versions=("capability.factory@1",),
            evidence_refs=(artifact_ref,),
            status="succeeded",
        )
        return outcome


@dataclass
class ScriptedMinibookClient:
    projects: list[dict[str, object]] = field(default_factory=list)
    posts: dict[str, dict[str, object]] = field(default_factory=dict)

    def ensure_projection_project(self, *, external_id: str) -> dict[str, object]:
        if not self.projects:
            self.projects.append(
                {
                    "id": external_id,
                    "name": MinibookProjector.PROJECTION_PROJECT,
                }
            )
        return self.projects[0]

    def list_projects(self) -> list[dict[str, object]]:
        return list(self.projects)

    def upsert_projection_post(self, project_id: str, *, event: object) -> dict[str, object]:
        event_id = str(event.event_id)  # type: ignore[attr-defined]
        post = self.posts.get(event_id)
        if post is None:
            post = {
                "id": f"post-{len(self.posts) + 1}",
                "project_id": project_id,
                "event_id": event_id,
            }
            self.posts[event_id] = post
        return post


@dataclass
class FullChainHarness:
    input_path: Path
    clock: MutableClock
    content_store: MemoryContentStore
    checkpoint_dir: Path
    gateway: ScriptedGatewayStore
    creation: ScriptedCreationPort
    releases: ScriptedReleaseRunPort
    sandbox: VerifiedScriptedSandboxRunner
    runtime: ScriptedRuntimePort
    minibook: ScriptedMinibookClient
    projector: MinibookProjector
    released_skill: ReleasedSkillRefV1

    def entrypoint(self) -> CapabilityFactoryEntrypoint:
        repository = GatewayFactoryRepository(self.gateway)  # real Package-C adapter
        return CapabilityFactoryEntrypoint(
            checkpoint_store=FileCapabilityFactoryCheckpointStore(self.checkpoint_dir),
            holdout_store=InMemoryPrivateHoldoutStore(),
            repository=repository,
            catalog=GatewayCapabilityCatalog(self.gateway),
            released_skill=self.released_skill,
            creation=self.creation,
            content_store=self.content_store,
            sandbox_runner=self.sandbox,
            evidence_issuer=self.releases,
            gateway=self.gateway,
            runtime=self.runtime,
            projector=self.projector,
            clock=self.clock,
        )


def _harness(tmp_path: Path) -> FullChainHarness:
    input_path = tmp_path / "TO_BE_BUILT.md"
    input_path.write_bytes(FIXTURE_INPUT.read_bytes())
    clock = MutableClock()
    content_store = MemoryContentStore()
    skill_ref = content_store.put(
        b"# Released Agent Factory skill\n",
        "text/markdown",
        namespace="released-skill",
    )
    released_skill = ReleasedSkillRefV1.model_validate(
        {
            "skill_id": "autogen-agent-factory",
            "version": 1,
            "content_ref": skill_ref.model_dump(mode="json"),
            "content_sha256": skill_ref.sha256,
        }
    )
    gateway = ScriptedGatewayStore(clock)
    creation = ScriptedCreationPort(
        store=content_store,
        gateway=gateway,
        released_skill_ref=skill_ref,
        crash_after_submit_once=True,
    )
    releases = ScriptedReleaseRunPort(
        creation=creation,
        crash_after_run_once=3,
    )
    sandbox = VerifiedScriptedSandboxRunner(clock=clock)
    runtime = ScriptedRuntimePort(gateway)
    minibook = ScriptedMinibookClient()
    projector = MinibookProjector(
        minibook,  # type: ignore[arg-type]
        ProjectionCursorStore(tmp_path / "projection-cursor.sqlite3"),
        owner_id="task-6-projector",
    )
    return FullChainHarness(
        input_path=input_path,
        clock=clock,
        content_store=content_store,
        checkpoint_dir=tmp_path / "factory-checkpoints",
        gateway=gateway,
        creation=creation,
        releases=releases,
        sandbox=sandbox,
        runtime=runtime,
        minibook=minibook,
        projector=projector,
        released_skill=released_skill,
    )


@pytest.mark.asyncio
async def test_complete_factory_release_chain_recovers_twice_without_duplicate_effects(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)

    with pytest.raises(SimulatedProcessCrash, match="creation submission"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    with pytest.raises(SimulatedProcessCrash, match="release run 3"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    summary = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )

    job = next(iter(harness.gateway.jobs.values()))
    creation_job = next(iter(harness.creation.submitted_jobs.values()))
    assert job.required_capability == "customer_support_triage"
    assert job.input_ref.sha256 == hashlib.sha256(harness.input_path.read_bytes()).hexdigest()
    assert summary.factory_job_id == job.job_id
    assert summary.invocation_job_id == job.job_id
    assert summary.release_authority_job_id == job.job_id
    assert summary.execution_mode == "created"
    assert summary.creation_job_id == creation_job.creation_job_id
    assert summary.terminal_state == "ready_to_use"
    assert summary.capability_id == job.required_capability
    assert summary.capability_version == 1
    assert summary.recovery_id == "controlled-recovery-01"
    assert summary.e2e_batch_ids == (
        "normal-e2e-01",
        "normal-e2e-02",
        "normal-e2e-03",
    )
    assert summary.execution_command_id in harness.gateway.executions
    assert summary.execution_result_id == harness.gateway.executions[
        summary.execution_command_id
    ].result_id
    assert len(summary.projection_event_ids) == 1
    assert len(harness.creation.submission_effects) == 1
    assert harness.releases.effects == [
        "controlled-recovery-01",
        "normal-e2e-01",
        "normal-e2e-02",
        "normal-e2e-03",
    ]
    assert len(harness.sandbox.requests) == 1
    assert len(harness.runtime.effects) == 1
    assert len(harness.gateway.release_effects) == 1
    assert len(harness.gateway.execution_effects) == 1
    assert len(harness.minibook.posts) == 1
    assert len(harness.gateway.blocks_by_job[job.job_id]) == 8
    assert harness.gateway.authority_order == ["promotion", "atomic-publication"]
    assert harness.gateway.runtime_authority_order == [
        "command",
        "grant",
        "claim",
        "result",
        "execution",
    ]


@pytest.mark.asyncio
async def test_checkpoint_identity_recovers_before_gateway_job_registration(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.gateway.crash_before_job_registration_once = True

    with pytest.raises(SimulatedProcessCrash, match="before Gateway job registration"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    checkpoint_path = next(harness.checkpoint_dir.glob("*.json"))
    checkpoint_bytes = checkpoint_path.read_bytes()
    checkpoint = CapabilityFactoryCheckpoint.model_validate_json(checkpoint_bytes)
    assert not harness.gateway.jobs
    harness.clock.value += timedelta(seconds=5)

    completed = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )

    registered = harness.gateway.jobs[checkpoint.factory_job_id]
    assert completed.execution_state == "completed"
    assert registered.occurred_at == checkpoint.occurred_at
    assert registered.deadline_at == checkpoint.deadline_at
    assert checkpoint_path.read_bytes() == checkpoint_bytes


@pytest.mark.asyncio
async def test_catalog_hit_reuses_frozen_authority_without_forge_or_republication(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    for _ in range(2):
        with pytest.raises(SimulatedProcessCrash):
            await harness.entrypoint().run(
                input_path=harness.input_path,
                correlation_id=CORRELATION_ID,
                subject_version=1,
                wall_clock_budget_seconds=600,
            )
    created = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )
    creation_effects = tuple(harness.creation.submission_effects)
    release_run_effects = tuple(harness.releases.effects)
    publication_effects = tuple(harness.gateway.release_effects)
    sandbox_effects = tuple(harness.sandbox.requests)
    reuse_correlation = UUID("f8bb07d4-ebf0-4544-a5c2-9d215886aa44")

    reused = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=reuse_correlation,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )

    assert reused.terminal_state == "ready_to_use"
    assert reused.execution_mode == "reused"
    assert reused.creation_job_id is None
    assert reused.capability_id == created.capability_id
    assert reused.capability_version == created.capability_version
    assert reused.correlation_id == reuse_correlation
    assert reused.invocation_job_id != created.invocation_job_id
    assert reused.release_authority_job_id == created.invocation_job_id
    assert reused.terminal_decision_id == created.terminal_decision_id
    assert reused.invocation_job_id in harness.gateway.jobs
    assert reused.execution_command_id in harness.gateway.executions
    assert harness.gateway.executions[reused.execution_command_id].correlation_id == reuse_correlation
    assert tuple(harness.creation.submission_effects) == creation_effects
    assert tuple(harness.releases.effects) == release_run_effects
    assert tuple(harness.gateway.release_effects) == publication_effects
    assert tuple(harness.sandbox.requests) == sandbox_effects
    assert len(harness.runtime.effects) == 2
    assert len(harness.gateway.execution_effects) == 2
    assert len(harness.minibook.posts) == 2


@pytest.mark.asyncio
async def test_claim_commit_crash_returns_typed_retry_then_reacquires_after_expiry(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.gateway.crash_after_claim_once = True

    with pytest.raises(SimulatedProcessCrash, match="runtime claim"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    provider_effects = harness.runtime.effects
    provider_executions = harness.runtime.executions
    harness.runtime = ScriptedRuntimePort(
        harness.gateway,
        effects=provider_effects,
        executions=provider_executions,
    )
    pending = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )
    claim = harness.gateway.runtime_claims[pending.execution_command_id]
    assert pending.execution_state == "retry_pending"
    assert pending.retry_expires_at == claim.expires_at
    assert pending.execution_result_id is None
    assert not harness.runtime.effects
    assert not harness.gateway.runtime_results
    checkpoint_text = next(harness.checkpoint_dir.glob("*.json")).read_text(
        encoding="utf-8"
    )
    assert "credential" not in checkpoint_text.casefold()

    harness.clock.value = claim.expires_at + timedelta(microseconds=1)
    harness.runtime = ScriptedRuntimePort(
        harness.gateway,
        effects=provider_effects,
    )
    completed = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )
    assert completed.execution_state == "completed"
    assert completed.execution_result_id is not None
    assert harness.gateway.runtime_claims[completed.execution_command_id].fencing_token == 2
    assert len(harness.runtime.effects) == 1
    assert len(harness.gateway.execution_effects) == 1


@pytest.mark.asyncio
async def test_result_commit_crash_uses_durable_receipt_without_provider_reexecution(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.gateway.crash_after_result_once = True

    with pytest.raises(SimulatedProcessCrash, match="runtime result"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    provider_effects = harness.runtime.effects
    provider_executions = harness.runtime.executions
    harness.runtime = ScriptedRuntimePort(
        harness.gateway,
        effects=provider_effects,
        executions=provider_executions,
    )
    summary = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )
    assert summary.execution_state == "completed"
    assert len(harness.runtime.effects) == 1
    assert len(harness.gateway.runtime_results) == 1
    assert len(harness.gateway.execution_effects) == 1
    assert len(harness.minibook.posts) == 1


@pytest.mark.parametrize(
    "origin_field",
    (
        "origin_claim_id",
        "origin_claim_fencing_token",
        "origin_claim_digest",
    ),
)
@pytest.mark.asyncio
async def test_normal_result_restart_rejects_receipt_from_another_claim(
    tmp_path: Path,
    origin_field: str,
) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.gateway.crash_after_result_once = True

    with pytest.raises(SimulatedProcessCrash, match="runtime result"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    effect_id, provider_execution = next(iter(harness.runtime.executions.items()))
    completed_claim = harness.gateway.runtime_claims[
        provider_execution.result.command_id
    ]
    invalid_origin = {
        "origin_claim_id": uuid5(completed_claim.claim_id, "wrong-origin"),
        "origin_claim_fencing_token": completed_claim.fencing_token + 1,
        "origin_claim_digest": "f" * 64,
    }
    changed_receipt = provider_execution.provider_receipt.model_copy(
        update={origin_field: invalid_origin[origin_field]}
    )
    provider_executions = dict(harness.runtime.executions)
    provider_executions[effect_id] = provider_execution.model_copy(
        update={"provider_receipt": changed_receipt}
    )
    harness.runtime = ScriptedRuntimePort(
        harness.gateway,
        effects=harness.runtime.effects,
        executions=provider_executions,
    )

    with pytest.raises(
        CapabilityProviderIdempotencyError,
        match="execution claim",
    ):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )


@pytest.mark.asyncio
async def test_provider_effect_crash_recovers_from_durable_receipt_without_duplicate(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.runtime.crash_after_provider_effect_once = True

    with pytest.raises(SimulatedProcessCrash, match="durable provider effect"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    provider_effects = harness.runtime.effects
    provider_executions = harness.runtime.executions
    assert len(provider_effects) == 1
    assert len(provider_executions) == 1
    assert not harness.gateway.runtime_results
    command_id = provider_effects[0]
    claim = harness.gateway.runtime_claims[command_id]

    harness.runtime = ScriptedRuntimePort(
        harness.gateway,
        effects=provider_effects,
        executions=provider_executions,
    )
    pending = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )
    assert pending.execution_state == "retry_pending"
    assert len(provider_effects) == 1

    harness.clock.value = claim.expires_at + timedelta(microseconds=1)
    harness.runtime = ScriptedRuntimePort(
        harness.gateway,
        effects=provider_effects,
        executions=provider_executions,
    )
    completed = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )

    assert completed.execution_state == "completed"
    assert len(provider_effects) == 1
    assert len(harness.gateway.runtime_results) == 1
    assert len(harness.gateway.execution_effects) == 1
    recovered_claim = harness.gateway.runtime_claims[command_id]
    gateway_result = harness.gateway.runtime_results[command_id]
    provider_execution = next(iter(provider_executions.values()))
    provider_result = provider_execution.result
    provider_receipt = provider_execution.provider_receipt
    receipt_digest = canonical_contract_sha256(provider_receipt)
    assert recovered_claim.fencing_token == 2
    assert gateway_result == provider_result
    assert gateway_result.model_dump_json(by_alias=True) == provider_result.model_dump_json(
        by_alias=True
    )
    recovery = harness.gateway.runtime_recoveries[command_id]
    assert gateway_result.occurred_at < recovered_claim.claimed_at
    assert recovery.event_id != provider_result.event_id
    assert recovered_claim.claimed_at <= recovery.observed_at < recovered_claim.expires_at
    assert recovery.command_id == provider_result.command_id
    assert recovery.original_result_id == provider_result.event_id
    assert recovery.original_result_digest == canonical_contract_sha256(provider_result)
    assert recovery.original_claim_id == provider_receipt.origin_claim_id
    assert recovery.original_claim_digest == provider_receipt.origin_claim_digest
    assert recovery.provider_effect_id == provider_receipt.effect_id
    assert recovery.provider_receipt_digest == receipt_digest
    assert recovery.original_claim_fence == 1
    assert recovery.recovery_claim_fence == 2
    assert recovery.correlation_id == provider_result.correlation_id
    assert recovery.causation_id == provider_result.event_id
    assert provider_receipt.result_digest == canonical_contract_sha256(
        provider_result
    )
    checkpoint_text = next(harness.checkpoint_dir.glob("*.json")).read_text(
        encoding="utf-8"
    )
    assert "credential" not in checkpoint_text.casefold()


@pytest.mark.asyncio
async def test_provider_effect_origin_survives_intermediate_recovery_claim_crash(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.runtime.crash_after_provider_effect_once = True

    with pytest.raises(SimulatedProcessCrash, match="durable provider effect"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    provider_effects = harness.runtime.effects
    provider_executions = harness.runtime.executions
    command_id = provider_effects[0]
    origin_claim = harness.gateway.runtime_claims[command_id]
    provider_execution = next(iter(provider_executions.values()))
    original_result_json = provider_execution.result.model_dump_json(by_alias=True)
    original_receipt_json = provider_execution.provider_receipt.model_dump_json(
        by_alias=True
    )

    harness.clock.value = origin_claim.expires_at + timedelta(microseconds=1)
    harness.gateway.crash_after_claim_once = True
    harness.runtime = ScriptedRuntimePort(
        harness.gateway,
        effects=provider_effects,
        executions=provider_executions,
    )
    with pytest.raises(SimulatedProcessCrash, match="after runtime claim"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    intermediate_claim = harness.gateway.runtime_claims[command_id]
    assert intermediate_claim.fencing_token == 2
    assert not harness.gateway.runtime_results
    harness.clock.value = intermediate_claim.expires_at + timedelta(microseconds=1)
    harness.runtime = ScriptedRuntimePort(
        harness.gateway,
        effects=provider_effects,
        executions=provider_executions,
    )
    completed = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )

    recovery_claim = harness.gateway.runtime_claims[command_id]
    recovery = harness.gateway.runtime_recoveries[command_id]
    gateway_result = harness.gateway.runtime_results[command_id]
    assert completed.execution_state == "completed"
    assert recovery_claim.fencing_token == 3
    assert provider_execution.provider_receipt.origin_claim_id == origin_claim.claim_id
    assert provider_execution.provider_receipt.origin_claim_fencing_token == 1
    assert provider_execution.provider_receipt.origin_claim_digest == canonical_contract_sha256(
        origin_claim
    )
    assert recovery.original_claim_id == origin_claim.claim_id
    assert recovery.original_claim_digest == canonical_contract_sha256(origin_claim)
    assert recovery.original_claim_fence == 1
    assert recovery.recovery_claim_fence == 3
    assert gateway_result.model_dump_json(by_alias=True) == original_result_json
    assert provider_execution.provider_receipt.model_dump_json(
        by_alias=True
    ) == original_receipt_json
    assert len(provider_effects) == 1


@pytest.mark.asyncio
async def test_provider_result_race_before_reclaim_uses_gateway_readback(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.runtime.crash_after_provider_effect_once = True

    with pytest.raises(SimulatedProcessCrash, match="durable provider effect"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    provider_effects = harness.runtime.effects
    provider_executions = harness.runtime.executions
    provider_execution = next(iter(provider_executions.values()))
    command_id = provider_execution.result.command_id
    claim = harness.gateway.runtime_claims[command_id]
    harness.gateway.result_committed_before_reclaim_once = provider_execution.result
    harness.clock.value = claim.expires_at + timedelta(microseconds=1)
    harness.runtime = ScriptedRuntimePort(
        harness.gateway,
        effects=provider_effects,
        executions=provider_executions,
    )

    completed = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )

    assert completed.execution_state == "completed"
    assert len(provider_effects) == 1
    assert len(harness.gateway.runtime_results) == 1
    assert len(harness.gateway.execution_effects) == 1
    assert harness.gateway.runtime_claims[command_id].fencing_token == 1


@pytest.mark.asyncio
async def test_runtime_without_durable_idempotency_is_refused_before_provider_effect(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.runtime.durable_idempotency_guaranteed = False

    with pytest.raises(CapabilityProviderIdempotencyError, match="idempotency"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    assert not harness.runtime.effects
    assert not harness.gateway.runtime_results


@pytest.mark.asyncio
async def test_projection_requires_exact_success_result_event(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.gateway.add_decoy_projection = True

    summary = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )

    assert summary.execution_result_id is not None
    assert summary.projection_event_ids == (summary.execution_result_id,)
    assert len(harness.gateway.projection_events(summary.correlation_id)) == 2
    assert len(harness.minibook.posts) == 1


@pytest.mark.asyncio
async def test_disk_checkpoint_recovers_after_publication_and_execution_commits(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.gateway.crash_after_publication_once = True
    harness.gateway.crash_after_execution_once = True

    with pytest.raises(SimulatedProcessCrash, match="atomic publication"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    with pytest.raises(SimulatedProcessCrash, match="capability execution"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    summary = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )

    assert summary.terminal_state == "ready_to_use"
    assert len(tuple(harness.checkpoint_dir.glob("*.json"))) == 1
    assert len(harness.creation.submission_effects) == 1
    assert len(harness.gateway.release_effects) == 1
    assert len(harness.runtime.effects) == 1
    assert len(harness.gateway.execution_effects) == 1
    assert len(harness.minibook.posts) == 1


@pytest.mark.asyncio
async def test_factory_resume_rejects_mutated_input_before_duplicate_effects(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)

    with pytest.raises(SimulatedProcessCrash, match="creation submission"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    harness.input_path.write_bytes(harness.input_path.read_bytes() + b"\n")

    with pytest.raises(CapabilityFactoryInputMutation, match="input bytes"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    assert len(harness.gateway.jobs) == 1
    assert len(harness.creation.submission_effects) == 1
    assert not harness.gateway.release_effects
    assert not harness.gateway.execution_effects


@pytest.mark.asyncio
async def test_required_tool_gap_blocks_release_and_execution(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.creation.required_gap = True

    summary = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )

    assert summary.terminal_state == "blocked"
    assert summary.unresolved_required_tool_gaps == ("missing-required-provider",)
    assert not harness.gateway.release_effects
    assert not harness.runtime.effects
    assert not harness.minibook.posts


@pytest.mark.asyncio
async def test_failed_private_holdout_rejects_package(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.creation.failed_holdout = True

    summary = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )

    assert summary.terminal_state == "rejected"
    assert not harness.gateway.release_effects
    assert not harness.runtime.effects
    assert not harness.minibook.posts


@pytest.mark.asyncio
async def test_expired_budget_stops_before_completion_and_release_effects(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.creation.advance_clock_after_result = timedelta(seconds=601)

    summary = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )

    job = next(iter(harness.gateway.jobs.values()))
    assert summary.terminal_state == "escalated"
    assert len(harness.gateway.blocks_by_job[job.job_id]) == 3
    assert not harness.releases.effects
    assert not harness.gateway.release_effects
    assert not harness.runtime.effects
    assert not harness.minibook.posts


@pytest.mark.asyncio
async def test_deadline_after_atomic_publication_stops_before_runtime_effects(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.gateway.advance_clock_after_publication = timedelta(seconds=601)

    with pytest.raises(CapabilityFactoryDeadlineExceeded, match="post-publication"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    assert len(harness.gateway.release_effects) == 1
    assert not harness.gateway.runtime_commands
    assert not harness.runtime.effects
    assert not harness.minibook.posts


@pytest.mark.asyncio
async def test_deadline_after_provider_execution_stops_before_result_mutation(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.runtime.advance_clock_after_execute = timedelta(seconds=601)

    with pytest.raises(CapabilityFactoryDeadlineExceeded, match="runtime result"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    assert len(harness.runtime.effects) == 1
    assert not harness.gateway.runtime_results
    assert not harness.gateway.execution_effects
    assert not harness.minibook.posts


@pytest.mark.asyncio
async def test_deadline_after_execution_commit_stops_before_projection_mutation(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.gateway.advance_clock_after_execution = timedelta(seconds=601)

    with pytest.raises(CapabilityFactoryDeadlineExceeded, match="Minibook"):
        await harness.entrypoint().run(
            input_path=harness.input_path,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            wall_clock_budget_seconds=600,
        )

    assert len(harness.gateway.execution_effects) == 1
    assert not harness.minibook.posts


@pytest.mark.asyncio
async def test_expired_validation_stops_before_lifecycle_evidence_request(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.sandbox.advance_clock_after_validate = timedelta(seconds=601)

    summary = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )

    assert summary.terminal_state != "ready_to_use"
    assert harness.releases.lifecycle_calls == 0
    assert not harness.gateway.release_effects


@pytest.mark.asyncio
async def test_rejected_artifact_digest_rejects_package(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.creation.rejected_archive = True

    summary = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )

    assert summary.terminal_state == "rejected"
    assert not harness.gateway.release_effects
    assert not harness.runtime.effects
    assert not harness.minibook.posts


@pytest.mark.asyncio
async def test_only_two_normal_successes_block_release(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.creation.crash_after_submit_once = False
    harness.releases.crash_after_run_once = None
    harness.releases.max_success_runs = 2

    summary = await harness.entrypoint().run(
        input_path=harness.input_path,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        wall_clock_budget_seconds=600,
    )

    assert summary.terminal_state == "blocked"
    assert summary.recovery_id == "controlled-recovery-01"
    assert summary.e2e_batch_ids == ("normal-e2e-01", "normal-e2e-02")
    assert not harness.sandbox.requests
    assert not harness.gateway.release_effects
    assert not harness.runtime.effects
    assert not harness.minibook.posts


SANDBOX_IMAGE = (
    "captain-capability-sandbox@sha256:"
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)


def _write_adapter_manifest(
    root: Path,
    *,
    source: str,
    factory_symbol: str = "build_entrypoint",
    module_sha256: str | None = None,
) -> tuple[Path, bytes, Path]:
    module_path = root / "adapters" / "capability_adapter.py"
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_content = source.encode("utf-8")
    module_path.write_bytes(module_content)
    manifest_content = json.dumps(
        {
            "schema": "captain.capability-factory-entrypoint-adapter-manifest.v1",
            "module_path": "adapters/capability_adapter.py",
            "module_sha256": (
                module_sha256
                if module_sha256 is not None
                else hashlib.sha256(module_content).hexdigest()
            ),
            "factory_symbol": factory_symbol,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    manifest_path = root / "adapter-manifest.json"
    manifest_path.write_bytes(manifest_content)
    return manifest_path, manifest_content, module_path


def _static_preflight_config(
    root: Path,
    manifest_path: Path,
    manifest_content: bytes,
) -> object:
    input_path = root / "TO_BE_BUILT.md"
    input_path.write_bytes(FIXTURE_INPUT.read_bytes())
    return parse_capability_factory_args(
        (
            "--input",
            "TO_BE_BUILT.md",
            "--sandbox-image",
            SANDBOX_IMAGE,
            "--correlation-id",
            str(CORRELATION_ID),
            "--preflight-only",
        ),
        environ={
            "CAPTAIN_GATEWAY_TOKEN": "gateway-secret-value",
            "CAPTAIN_RUNTIME_TOKEN": "runtime-secret-value",
            "MINIBOOK_PROJECTION_API_KEY": "projection-secret-value",
            "CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_MANIFEST": manifest_path.name,
            "CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_SHA256": hashlib.sha256(
                manifest_content
            ).hexdigest(),
        },
        workspace_root=root,
    )


@dataclass
class RecordingDockerCommandRunner:
    network_mode: str = "none"
    commands: list[tuple[str, ...]] = field(default_factory=list)
    started: bool = False

    async def run(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float | None = None,
    ) -> DockerCommandResult:
        self.commands.append(arguments)
        if arguments[:2] == ("image", "inspect"):
            return DockerCommandResult(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Id": "sha256:" + "1" * 64,
                            "RepoDigests": [SANDBOX_IMAGE],
                        }
                    ]
                ),
                stderr="",
            )
        if arguments[0] == "create":
            return DockerCommandResult(
                returncode=0,
                stdout="f" * 64 + "\n",
                stderr="",
            )
        if arguments[0] == "start":
            self.started = True
            return DockerCommandResult(returncode=0, stdout="", stderr="")
        if arguments[0] == "inspect":
            workspace = next(
                item.split("source=", 1)[1].split(",target=", 1)[0]
                for command in self.commands
                for item in command
                if item.startswith("type=bind,source=")
            )
            return DockerCommandResult(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Id": "f" * 64,
                            "Name": "/captain-capability-" + str(CORRELATION_ID),
                            "Config": {
                                "Image": SANDBOX_IMAGE,
                                "User": "65532:65532",
                                "WorkingDir": "/workspace",
                                "Entrypoint": ["python"],
                                "Labels": {
                                    "captain.owner": "capability-factory",
                                    "captain.disposable": "true",
                                },
                            },
                            "HostConfig": {
                                "NetworkMode": self.network_mode,
                                "ReadonlyRootfs": True,
                                "Memory": 512 * 1024 * 1024,
                                "PidsLimit": 8,
                                "CapDrop": ["ALL"],
                                "SecurityOpt": ["no-new-privileges"],
                                "Init": True,
                                "Tmpfs": {
                                    "/tmp": "rw,noexec,nosuid,size=67108864"
                                },
                            },
                            "Mounts": [
                                {
                                    "Type": "bind",
                                    "Source": workspace,
                                    "Destination": "/workspace",
                                    "RW": False,
                                }
                            ],
                            "State": {
                                "Status": "exited" if self.started else "created",
                                "ExitCode": 0,
                                "Running": False,
                            },
                        }
                    ]
                ),
                stderr="",
            )
        if arguments[:2] == ("rm", "-f"):
            return DockerCommandResult(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker arguments: {arguments[0]}")


@pytest.mark.asyncio
async def test_docker_cli_runner_enforces_timeout_and_terminates_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = asyncio.Event()

    class HangingProcess:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await stopped.wait()
            return b"", b""

        def terminate(self) -> None:
            self.returncode = -15
            stopped.set()

        def kill(self) -> None:
            self.returncode = -9
            stopped.set()

    process = HangingProcess()

    async def create_subprocess(*args: object, **kwargs: object) -> HangingProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)

    with pytest.raises(CapabilitySandboxIsolationError, match="timed out"):
        await DockerCliCommandRunner().run(("version",), timeout_seconds=0.001)

    assert process.returncode == -15


def _sandbox_request(tmp_path: Path) -> CapabilitySandboxRequest:
    (tmp_path / "autogen").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "autogen" / "team.py").write_text(
        "CAPABILITY_ID = 'customer_support_triage'\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_team.py").write_text(
        "def test_team(): assert True\n",
        encoding="utf-8",
    )
    execution_id = CORRELATION_ID
    entries = []
    for path in tmp_path.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            entries.append(
                (
                    path.relative_to(tmp_path).as_posix(),
                    hashlib.sha256(data).hexdigest(),
                    len(data),
                )
            )
    tree_digest = hashlib.sha256(
        json.dumps(sorted(entries), separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CapabilitySandboxRequest(
        request_digest="a" * 64,
        execution_id=execution_id,
        process_identity=f"sandbox-handle://{execution_id}",
        correlation_id=CORRELATION_ID,
        workspace=tmp_path,
        python_path_root=tmp_path,
        module_names=("autogen.team",),
        test_paths=("tests/test_team.py",),
        extracted_tree_sha256=tree_digest,
        package_archive_sha256="c" * 64,
        timeout_seconds=30,
    )


@pytest.mark.asyncio
async def test_docker_sandbox_attests_only_inspected_isolation(tmp_path: Path) -> None:
    command_runner = RecordingDockerCommandRunner()
    request = _sandbox_request(tmp_path)
    runner = DockerCapabilitySandboxRunner(
        image=SANDBOX_IMAGE,
        command_runner=command_runner,
    )

    result = await runner.validate(request)

    assert result.status == "passed"
    assert result.process_identity == request.process_identity
    assert result.sandbox_identity == "sandbox://docker/" + "f" * 64
    create = next(command for command in command_runner.commands if command[0] == "create")
    assert ("--network", "none") == create[create.index("--network") :][:2]
    assert "--read-only" in create
    assert "--init" in create
    assert ("--cap-drop", "ALL") == create[create.index("--cap-drop") :][:2]
    assert ("--security-opt", "no-new-privileges") == create[
        create.index("--security-opt") :
    ][:2]
    assert ("--pull", "never") == create[create.index("--pull") :][:2]
    assert "/tmp:rw,noexec,nosuid,size=67108864" in create
    assert any('sys.path.insert(0, "/workspace")' in item for item in create)
    assert any(item.endswith(",readonly") for item in create)


@pytest.mark.asyncio
async def test_docker_sandbox_fails_closed_when_inspection_disagrees(tmp_path: Path) -> None:
    command_runner = RecordingDockerCommandRunner(network_mode="bridge")
    runner = DockerCapabilitySandboxRunner(
        image=SANDBOX_IMAGE,
        command_runner=command_runner,
    )

    with pytest.raises(CapabilitySandboxIsolationError, match="network"):
        await runner.validate(_sandbox_request(tmp_path))

    assert not command_runner.started


def test_cli_uses_safe_paths_service_urls_and_environment_only_secrets(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "TO_BE_BUILT.md"
    input_path.write_bytes(FIXTURE_INPUT.read_bytes())
    adapter_manifest, adapter_manifest_content, adapter_module = (
        _write_adapter_manifest(
            tmp_path,
            source="def build_entrypoint(config):\n    return config\n",
        )
    )
    environment = {
        "CAPTAIN_GATEWAY_TOKEN": "gateway-secret-value",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-secret-value",
        "MINIBOOK_PROJECTION_API_KEY": "projection-secret-value",
        "CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_MANIFEST": "adapter-manifest.json",
        "CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_SHA256": hashlib.sha256(
            adapter_manifest_content
        ).hexdigest(),
    }

    config = parse_capability_factory_args(
        (
            "--input",
            "TO_BE_BUILT.md",
            "--artifact-dir",
            "artifacts/capability-factory",
            "--checkpoint-dir",
            ".superpowers/sdd/checkpoints",
            "--gateway-url",
            "http://127.0.0.1:8080",
            "--runtime-url",
            "http://127.0.0.1:8081",
            "--minibook-url",
            "http://127.0.0.1:8082",
            "--sandbox-image",
            SANDBOX_IMAGE,
            "--correlation-id",
            str(CORRELATION_ID),
        ),
        environ=environment,
        workspace_root=tmp_path,
    )

    assert config.input_path == input_path.resolve()
    assert config.artifact_dir.is_relative_to(tmp_path.resolve())
    assert config.checkpoint_dir.is_relative_to(tmp_path.resolve())
    assert config.gateway_url == "http://127.0.0.1:8080"
    assert config.sandbox_image == SANDBOX_IMAGE
    assert config.adapter_manifest_path == adapter_manifest.resolve()
    assert adapter_module.is_relative_to(tmp_path.resolve())
    rendered = repr(config)
    assert "gateway-secret-value" not in rendered
    assert "runtime-secret-value" not in rendered
    assert "projection-secret-value" not in rendered


@pytest.mark.asyncio
async def test_static_preflight_never_imports_or_instantiates_adapter(
    tmp_path: Path,
) -> None:
    import_marker = tmp_path / "adapter-imported.txt"
    manifest_path, manifest_content, _ = _write_adapter_manifest(
        tmp_path,
        source=(
            "from pathlib import Path\n"
            f"Path({str(import_marker)!r}).write_text('imported', encoding='utf-8')\n"
            "def build_entrypoint(config): raise AssertionError('must not instantiate')\n"
        ),
    )
    config = _static_preflight_config(tmp_path, manifest_path, manifest_content)

    result = await run_capability_factory_cli(config)

    assert result["status"] == "preflight_ok"
    assert not import_marker.exists()


@pytest.mark.asyncio
async def test_static_preflight_rejects_adapter_module_digest_mismatch(
    tmp_path: Path,
) -> None:
    manifest_path, manifest_content, _ = _write_adapter_manifest(
        tmp_path,
        source="def build_entrypoint(config):\n    return config\n",
        module_sha256="0" * 64,
    )
    config = _static_preflight_config(tmp_path, manifest_path, manifest_content)

    with pytest.raises(CapabilityFactoryConfigurationError, match="module digest"):
        await run_capability_factory_cli(config)


@pytest.mark.asyncio
async def test_static_preflight_rejects_missing_factory_symbol(tmp_path: Path) -> None:
    manifest_path, manifest_content, _ = _write_adapter_manifest(
        tmp_path,
        source="def another_symbol(config):\n    return config\n",
    )
    config = _static_preflight_config(tmp_path, manifest_path, manifest_content)

    with pytest.raises(CapabilityFactoryConfigurationError, match="factory symbol"):
        await run_capability_factory_cli(config)


@pytest.mark.asyncio
async def test_static_preflight_rejects_adapter_path_outside_workspace(
    tmp_path: Path,
) -> None:
    manifest_content = json.dumps(
        {
            "schema": "captain.capability-factory-entrypoint-adapter-manifest.v1",
            "module_path": "../outside-adapter.py",
            "module_sha256": "a" * 64,
            "factory_symbol": "build_entrypoint",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    manifest_path = tmp_path / "adapter-manifest.json"
    manifest_path.write_bytes(manifest_content)
    config = _static_preflight_config(tmp_path, manifest_path, manifest_content)

    with pytest.raises(CapabilityFactoryConfigurationError, match="workspace"):
        await run_capability_factory_cli(config)


def test_cli_rejects_credential_flags_and_unsafe_paths_without_echo(
    tmp_path: Path,
) -> None:
    secret = "must-not-appear-in-error"
    with pytest.raises(CapabilityFactoryConfigurationError) as credential_error:
        parse_capability_factory_args(
            ("--gateway-token", secret),
            environ={},
            workspace_root=tmp_path,
        )
    assert secret not in str(credential_error.value)

    with pytest.raises(CapabilityFactoryConfigurationError, match="workspace"):
        parse_capability_factory_args(
            ("--input", "../outside.md"),
            environ={},
            workspace_root=tmp_path,
        )


def test_redacted_evidence_manifest_is_content_addressed(tmp_path: Path) -> None:
    summary = CapabilityFactoryRunSummary(
        correlation_id=CORRELATION_ID,
        factory_job_id=FACTORY_JOB_ID,
        invocation_job_id=FACTORY_JOB_ID,
        execution_mode="created",
        execution_state="not_started",
        creation_job_id=uuid5(FACTORY_JOB_ID, "creation"),
        terminal_decision_id=uuid5(FACTORY_JOB_ID, "terminal"),
        terminal_state="blocked",
        capability_id="customer_support_triage",
        recovery_id="controlled-recovery-01",
        e2e_batch_ids=("normal-e2e-01", "normal-e2e-02"),
        release_evidence_sha256=("d" * 64, "e" * 64, "f" * 64),
    )

    path = write_redacted_evidence_manifest(summary, tmp_path)
    content = path.read_bytes()

    assert path.name == hashlib.sha256(content).hexdigest() + ".json"
    assert json.loads(content)["summary"]["terminal_state"] == "blocked"
    assert str(tmp_path).encode("utf-8") not in content
    assert b"http://" not in content
    assert b"token" not in content.lower()


def test_live_gate_declares_exact_order_and_preserves_vibemind() -> None:
    script = (
        Path(__file__).parents[2] / "scripts" / "run-capability-factory-live.ps1"
    ).read_text(encoding="utf-8")
    markers = tuple(f"# STEP {number}:" for number in range(1, 11))

    positions = tuple(script.index(marker) for marker in markers)
    assert positions == tuple(sorted(positions))
    assert "$env:CAPABILITY_FACTORY_LIVE_REQUIRED = '1'" in script
    assert "[switch]$UseManagedGateway" in script
    assert '"$GatewayUrl/batches?status=READY"' in script
    assert "CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_MANIFEST" in script
    assert "CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_SHA256" in script
    assert "CAPABILITY_FACTORY_ADAPTER_FACTORY" not in script
    assert script.index('$null = Invoke-PythonFactory ($commonArguments + \'--preflight-only\')') < script.index("# STEP 3:")
    assert script.index('$RuntimeUrl/health') < script.index('$runJson = Invoke-PythonFactory')
    assert script.index('$MinibookUrl/health') < script.index('$runJson = Invoke-PythonFactory')
    assert "docker compose down" not in script.casefold()
    assert "down --volumes" not in script.casefold()
    assert "down -v" not in script.casefold()
    assert "vibemind" not in script.casefold()

"""Captain-owned technical failure evaluation and rebuild authority storage."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from agenten.agent_factory.candidate_evaluation import (
    FactoryCandidateEvaluationResult,
)
from agenten.agent_factory.contracts import (
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryJob,
    FactoryPhase,
)
from agenten.agent_factory.outcome_contracts import AssertionOutcome
from agenten.agent_factory.skill_sequence import (
    FactoryImprovementAuthorizationV1,
    build_factory_improvement_authorization,
    validate_factory_improvement_authorization,
)
from agenten.agent_factory.skill_workflow_contracts import (
    FactoryFeedbackRecommendation,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.service import FactoryCoordinator
from agenten.agent_factory.state_machine import (
    FactoryAction,
    FactoryActionKind,
    FactoryProjection,
)
from agenten.agent_factory.technical_improvement_contracts import (
    CaptainTechnicalFailureEvaluationV1,
    TechnicalFailureDiagnosticCode,
    build_captain_technical_failure_evaluation,
)
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent


class _ImprovementRepository(Protocol):
    def job(self, job_id: UUID) -> FactoryJob: ...

    def blocks(self, job_id: UUID) -> tuple[FactoryEvidenceBlock, ...]: ...

    def workflow_artifacts(self, job_id: UUID) -> tuple[object, ...]: ...


class _CurrentCandidateSource(Protocol):
    def current_candidate_ref(
        self, job: FactoryJob, attempt: int
    ) -> ArtifactRef | None: ...


class _FactoryEvidenceReader(Protocol):
    def read_verified(
        self, reference: ArtifactRef, *, job_id: UUID | None = None
    ) -> bytes: ...


class CaptainTechnicalFailureEvaluator:
    """Convert immutable pre-benchmark failures into public-safe retry inputs."""

    def from_build_failure(
        self,
        *,
        job: FactoryJob,
        source_block: FactoryEvidenceBlock,
        candidate_ref: ArtifactRef,
        result: FactoryCandidateEvaluationResult,
        evidence_ref: ArtifactRef,
        occurred_at: datetime,
    ) -> CaptainTechnicalFailureEvaluationV1:
        _require_source(
            job=job,
            source_block=source_block,
            source_phase=FactoryPhase.BUILD_FAILED,
            occurred_at=occurred_at,
        )
        failed_checks = tuple(
            check for check in result.checks if check.status == "failed"
        )
        if (
            source_block.status is not FactoryBlockStatus.FAILED
            or evidence_ref not in source_block.evidence_refs
            or result.status != "failed"
            or any(check.status == "infrastructure_failed" for check in result.checks)
            or not any(
                check.name in {"build", "real_case"}
                for check in failed_checks
            )
        ):
            raise ValueError("build failure evidence is not retry-eligible")
        if (
            result.candidate_manifest is not None
            and result.candidate_manifest.source_archive_ref != candidate_ref
        ):
            raise ValueError("build failure candidate binding does not match")
        outcomes = tuple(
            AssertionOutcome(
                assertion_id=assertion_id,
                status="failed",
                integration_intent=IntegrationIntent.NONE,
                evidence_refs=(evidence_ref,),
            )
            for assertion_id in job.acceptance_assertion_ids
        )
        return build_captain_technical_failure_evaluation(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=source_block.attempt,
            source_phase=source_block.phase,
            source_block_id=source_block.event_id,
            occurred_at=occurred_at,
            candidate_ref=candidate_ref,
            acceptance_assertion_ids=job.acceptance_assertion_ids,
            assertion_outcomes=outcomes,
            evidence_refs=(evidence_ref,),
            technical_diagnostic_codes=_technical_diagnostic_codes(result),
            failure_class="test_regression",
            recommendation=FactoryFeedbackRecommendation.RETRY_BUILD,
        )
    def from_team_execution(
        self,
        *,
        job: FactoryJob,
        source_block: FactoryEvidenceBlock,
        candidate_ref: ArtifactRef,
        execution: TeamExecutionEvidenceV1,
        occurred_at: datetime,
    ) -> CaptainTechnicalFailureEvaluationV1:
        if source_block.phase not in {
            FactoryPhase.REAL_CASE_EVIDENCE,
            FactoryPhase.REAL_CASE_REVALIDATED,
        }:
            raise ValueError("technical execution source phase is not retry-eligible")
        _require_source(
            job=job,
            source_block=source_block,
            source_phase=source_block.phase,
            occurred_at=occurred_at,
        )
        outcomes = execution.execution_outcome.assertion_outcomes
        if (
            execution.job_id != job.job_id
            or execution.correlation_id != job.correlation_id
            or execution.subject_version != job.subject_version
            or execution.attempt != source_block.attempt
            or execution.candidate_ref != candidate_ref
            or execution.acceptance_assertion_ids != job.acceptance_assertion_ids
            or tuple(item.assertion_id for item in outcomes)
            != job.acceptance_assertion_ids
            or execution.status != "unresolved"
            or not any(item.status == "failed" for item in outcomes)
            or execution.artifact_ref
            not in (*source_block.artifact_refs, *source_block.evidence_refs)
        ):
            raise ValueError("technical execution evidence is not retry-eligible")
        evidence_refs = tuple(
            dict.fromkeys((execution.artifact_ref, *execution.evidence_refs))
        )
        normalized = tuple(
            outcome.model_copy(update={"evidence_refs": evidence_refs})
            for outcome in outcomes
        )
        diagnostic_codes = tuple(
            code
            for assertion_id, code in (
                ("business_value", "business_value_failed"),
                ("mandatory_handoff", "mandatory_handoff_failed"),
            )
            if any(
                outcome.assertion_id == assertion_id
                and outcome.status == "failed"
                for outcome in outcomes
            )
        )
        return build_captain_technical_failure_evaluation(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=source_block.attempt,
            source_phase=source_block.phase,
            source_block_id=source_block.event_id,
            occurred_at=occurred_at,
            candidate_ref=candidate_ref,
            acceptance_assertion_ids=job.acceptance_assertion_ids,
            assertion_outcomes=normalized,
            evidence_refs=evidence_refs,
            technical_diagnostic_codes=diagnostic_codes,
            failure_class="behavioral_failure",
            recommendation=FactoryFeedbackRecommendation.RETRY_BUILD,
        )


def _technical_diagnostic_codes(
    result: FactoryCandidateEvaluationResult,
) -> tuple[TechnicalFailureDiagnosticCode, ...]:
    """Map public evaluator messages to a bounded, prompt-safe repair vocabulary."""

    if any(
        check.name == "build" and check.status == "failed"
        for check in result.checks
    ):
        return ("candidate_build_command_failed",)
    failed = tuple(
        check
        for check in result.checks
        if check.name == "real_case" and check.status == "failed"
    )
    if len(failed) != 1:
        return ("real_case_contract_failed",)
    detail = failed[0].detail
    if detail.startswith("candidate command failed:"):
        return ("real_case_command_failed",)
    if detail == "real-case command must emit exactly one JSON object":
        return ("real_case_output_not_json",)
    if detail == "real-case result does not carry the Captain trace ID":
        return ("real_case_trace_id_mismatch",)
    if detail in {
        "real-case result must contain non-empty assertion_ids",
        "real-case result assertion_ids must be unique",
    }:
        return ("real_case_assertion_ids_invalid",)
    if detail == (
        "real-case result does not prove exactly the Captain acceptance assertions"
    ):
        return ("real_case_assertion_ids_mismatch",)
    return ("real_case_contract_failed",)


class CaptainFactoryImprovementAuthorizationStore:
    """Write technical evaluations and their exact authorization once."""

    def __init__(self, root: Path) -> None:
        self._root = _private_root(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def persist(
        self,
        authorization: FactoryImprovementAuthorizationV1,
    ) -> FactoryImprovementAuthorizationV1:
        validated = validate_factory_improvement_authorization(authorization)
        evaluation = validated.failed_evaluation
        if not isinstance(evaluation, CaptainTechnicalFailureEvaluationV1):
            raise ValueError("technical improvement store requires Captain evaluation")
        evaluation_path = (
            self._root
            / "evaluations"
            / str(evaluation.job_id)
            / f"{evaluation.artifact_ref.sha256}.json"
        ).resolve()
        authorization_path = (
            self._root
            / "authorizations"
            / str(evaluation.job_id)
            / f"{validated.authorization_ref.sha256}.json"
        ).resolve()
        _require_within(evaluation_path, self._root)
        _require_within(authorization_path, self._root)
        _write_once(evaluation_path, _canonical_model(evaluation))
        _write_once(authorization_path, _canonical_model(validated))
        return validated

    def existing(
        self,
        *,
        job: FactoryJob,
        projection: FactoryProjection,
    ) -> FactoryImprovementAuthorizationV1:
        matches = _matching_authorizations(
            root=self._root,
            job=job,
            authorized_attempt=projection.attempt,
            block_ids=projection.block_ids,
        )
        if len(matches) != 1:
            raise ValueError(
                "Captain improvement authority is unavailable"
                if not matches
                else "Captain improvement authority is conflicting"
            )
        return matches[0]


class CaptainTechnicalImprovementIssuer:
    """Append one Captain request derived from the current failed technical gate."""

    def __init__(
        self,
        *,
        repository: _ImprovementRepository,
        coordinator: FactoryCoordinator,
        candidates: _CurrentCandidateSource,
        evidence: _FactoryEvidenceReader,
        authorizations: CaptainFactoryImprovementAuthorizationStore,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._coordinator = coordinator
        self._candidates = candidates
        self._evidence = evidence
        self._authorizations = authorizations
        self._clock = clock
        self._evaluator = CaptainTechnicalFailureEvaluator()

    def issue(self, job_id: UUID) -> FactoryImprovementAuthorizationV1:
        projection = self._coordinator.projection(job_id)
        job = projection.job
        now = self._clock()
        _require_utc(now, "improvement issuance")
        deadline = getattr(job, "deadline_at", None)
        if deadline is not None and now >= deadline:
            raise ValueError("improvement issuance is outside the job deadline")
        if projection.phase is FactoryPhase.IMPROVEMENT_REQUESTED:
            return self._authorizations.existing(
                job=job,
                projection=projection,
            )
        if projection.phase not in {
            FactoryPhase.BUILD_FAILED,
            FactoryPhase.REAL_CASE_EVIDENCE,
            FactoryPhase.REAL_CASE_REVALIDATED,
        }:
            raise ValueError("current Factory phase is not retry-eligible")
        source_blocks = tuple(
            block
            for block in self._repository.blocks(job_id)
            if block.phase is projection.phase and block.attempt == projection.attempt
        )
        if not source_blocks or source_blocks[-1].status is not FactoryBlockStatus.FAILED:
            raise ValueError("latest technical failure source block is unavailable")
        source_block = source_blocks[-1]
        candidate_ref = self._candidates.current_candidate_ref(
            job,
            projection.attempt,
        )
        if candidate_ref is None:
            raise ValueError("technical failure candidate is unavailable")
        if projection.phase is FactoryPhase.BUILD_FAILED:
            evaluation = self._evaluate_build(
                job=job,
                source_block=source_block,
                candidate_ref=candidate_ref,
                occurred_at=now,
            )
        else:
            evaluation = self._evaluate_execution(
                job=job,
                source_block=source_block,
                candidate_ref=candidate_ref,
                occurred_at=now,
            )
        request = FactoryEvidenceBlock(
            schema_name="captain.agent-factory-block.v1",
            event_id=uuid5(
                NAMESPACE_URL,
                "|".join(
                    (
                        "factory-technical-improvement",
                        str(job.job_id),
                        str(job.subject_version),
                        str(projection.attempt),
                        source_block.event_id.hex,
                        evaluation.artifact_ref.sha256,
                        candidate_ref.sha256,
                    )
                ),
            ),
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            causation_id=source_block.event_id,
            occurred_at=now,
            producer="captain",
            subject_version=job.subject_version,
            attempt=projection.attempt,
            phase=FactoryPhase.IMPROVEMENT_REQUESTED,
            status=FactoryBlockStatus.SUCCEEDED,
            artifact_refs=(candidate_ref,),
            evidence_refs=tuple(
                dict.fromkeys((evaluation.artifact_ref, *evaluation.evidence_refs))
            ),
            assertion_ids=evaluation.prior_green_regression_ids,
        )
        authorization = build_factory_improvement_authorization(
            request_block=request,
            failed_evaluation=evaluation,
            prior_candidate_ref=candidate_ref,
        )
        self._authorizations.persist(authorization)
        self._coordinator.record(request)
        return authorization

    def _evaluate_build(
        self,
        *,
        job: FactoryJob,
        source_block: FactoryEvidenceBlock,
        candidate_ref: ArtifactRef,
        occurred_at: datetime,
    ) -> CaptainTechnicalFailureEvaluationV1:
        candidates: list[tuple[FactoryCandidateEvaluationResult, ArtifactRef]] = []
        for reference in source_block.evidence_refs:
            try:
                parsed = FactoryCandidateEvaluationResult.model_validate_json(
                    self._evidence.read_verified(reference, job_id=job.job_id)
                )
            except (OSError, ValueError):
                continue
            if parsed.status == "failed":
                candidates.append((parsed, reference))
        if len(candidates) != 1:
            raise ValueError("failed candidate evaluation evidence is ambiguous")
        result, evidence_ref = candidates[0]
        return self._evaluator.from_build_failure(
            job=job,
            source_block=source_block,
            candidate_ref=candidate_ref,
            result=result,
            evidence_ref=evidence_ref,
            occurred_at=occurred_at,
        )

    def _evaluate_execution(
        self,
        *,
        job: FactoryJob,
        source_block: FactoryEvidenceBlock,
        candidate_ref: ArtifactRef,
        occurred_at: datetime,
    ) -> CaptainTechnicalFailureEvaluationV1:
        resolved: dict[tuple[UUID, ArtifactRef], TeamExecutionEvidenceV1] = {}
        for artifact in self._repository.workflow_artifacts(job.job_id):
            if isinstance(artifact, TeamExecutionEvidenceV1):
                _add_exact_execution(resolved, artifact)
        for reference in source_block.evidence_refs:
            try:
                artifact = TeamExecutionEvidenceV1.model_validate_json(
                    self._evidence.read_verified(reference, job_id=job.job_id)
                )
            except (OSError, ValueError):
                continue
            _add_exact_execution(resolved, artifact)
        executions = tuple(
            artifact
            for artifact in resolved.values()
            if artifact.attempt == source_block.attempt
            and artifact.artifact_ref
            in (*source_block.artifact_refs, *source_block.evidence_refs)
        )
        if len(executions) != 1:
            raise ValueError("technical execution evidence is ambiguous")
        return self._evaluator.from_team_execution(
            job=job,
            source_block=source_block,
            candidate_ref=candidate_ref,
            execution=executions[0],
            occurred_at=occurred_at,
        )


def _add_exact_execution(
    resolved: dict[tuple[UUID, ArtifactRef], TeamExecutionEvidenceV1],
    execution: TeamExecutionEvidenceV1,
) -> None:
    key = (execution.invocation_id, execution.artifact_ref)
    existing = resolved.get(key)
    if existing is not None and existing != execution:
        raise ValueError("technical execution evidence binding conflicts")
    resolved[key] = execution


class FilesystemFactoryImprovementAuthority:
    """Return only the content-bound authority present in the current ledger."""

    def __init__(self, root: Path) -> None:
        self._root = _private_root(root)

    def active(
        self,
        job: FactoryJob,
        action: FactoryAction,
        projection: FactoryProjection,
        now: datetime,
    ) -> FactoryImprovementAuthorizationV1:
        _require_utc(now, "improvement authority lookup")
        if (
            action.kind is not FactoryActionKind.DISPATCH_TOOL_INTEGRATOR
            or action.attempt < 2
            or projection.job != job
            or projection.attempt != action.attempt
            or projection.phase is not FactoryPhase.BLUEPRINT_CREATED
        ):
            raise ValueError("improvement authority dispatch binding does not match")
        deadline = getattr(job, "deadline_at", None)
        if deadline is not None and now >= deadline:
            raise ValueError("improvement authority is outside the job deadline")
        matches = _matching_authorizations(
            root=self._root,
            job=job,
            authorized_attempt=action.attempt,
            block_ids=projection.block_ids,
        )
        if not matches and _has_matching_authorization_without_block(
            root=self._root,
            job=job,
            authorized_attempt=action.attempt,
        ):
            raise ValueError("improvement authority request block is not in the ledger")
        if len(matches) != 1:
            raise ValueError(
                "Captain improvement authority is unavailable"
                if not matches
                else "Captain improvement authority is conflicting"
            )
        return matches[0]


def _matching_authorizations(
    *,
    root: Path,
    job: FactoryJob,
    authorized_attempt: int,
    block_ids: tuple[UUID, ...],
) -> tuple[FactoryImprovementAuthorizationV1, ...]:
    return tuple(
        authorization
        for authorization in _authorizations_for_job(root, job.job_id)
        if authorization.request_block.job_id == job.job_id
        and authorization.request_block.correlation_id == job.correlation_id
        and authorization.request_block.subject_version == job.subject_version
        and authorization.authorized_attempt == authorized_attempt
        and authorization.request_block.event_id in block_ids
    )


def _has_matching_authorization_without_block(
    *,
    root: Path,
    job: FactoryJob,
    authorized_attempt: int,
) -> bool:
    return any(
        authorization.request_block.job_id == job.job_id
        and authorization.request_block.correlation_id == job.correlation_id
        and authorization.request_block.subject_version == job.subject_version
        and authorization.authorized_attempt == authorized_attempt
        for authorization in _authorizations_for_job(root, job.job_id)
    )


def _authorizations_for_job(
    root: Path,
    job_id: UUID,
) -> tuple[FactoryImprovementAuthorizationV1, ...]:
    directory = (root / "authorizations" / str(job_id)).resolve()
    _require_within(directory, root)
    if not directory.is_dir():
        return ()
    return tuple(
        _read_authorization(path)
        for path in sorted(directory.glob("*.json"))
        if path.is_file()
    )


def _require_source(
    *,
    job: FactoryJob,
    source_block: FactoryEvidenceBlock,
    source_phase: FactoryPhase,
    occurred_at: datetime,
) -> None:
    _require_utc(occurred_at, "technical failure evaluation")
    if (
        source_block.phase is not source_phase
        or source_block.job_id != job.job_id
        or source_block.correlation_id != job.correlation_id
        or source_block.subject_version != job.subject_version
        or source_block.attempt < 1
        or occurred_at < source_block.occurred_at
    ):
        raise ValueError("technical failure source binding does not match")


def _read_authorization(path: Path) -> FactoryImprovementAuthorizationV1:
    try:
        authorization = FactoryImprovementAuthorizationV1.model_validate_json(
            path.read_bytes()
        )
        return validate_factory_improvement_authorization(authorization)
    except (OSError, ValueError) as exc:
        raise ValueError("Captain improvement authority is invalid") from exc


def _canonical_model(model: object) -> bytes:
    payload = model.model_dump(  # type: ignore[attr-defined]
        mode="json",
        by_alias=True,
        exclude_none=True,
    )
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _private_root(path: Path) -> Path:
    root = path.resolve()
    if ".captain-cook" not in {part.casefold() for part in root.parts}:
        raise ValueError("improvement authority must use the private .captain-cook namespace")
    return root


def _require_within(path: Path, root: Path) -> None:
    if not path.is_relative_to(root):
        raise ValueError("improvement authority path escapes its root")


def _write_once(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            path,
            getattr(os, "O_BINARY", 0) | os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValueError("improvement authority immutable binding changed")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{label} time must be UTC")


__all__ = [
    "CaptainFactoryImprovementAuthorizationStore",
    "CaptainTechnicalImprovementIssuer",
    "CaptainTechnicalFailureEvaluator",
    "FilesystemFactoryImprovementAuthority",
]

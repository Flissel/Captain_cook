from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from agenten.agent_factory.candidate_evaluation import (
    FactoryCandidateEvaluator,
    FactoryCandidateManifest,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.evidence_store import FilesystemSkillEvaluationEvidenceStore
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_factory.orchestration import (
    FactoryDispatchError,
    HermesSkillEvaluationCoordinator,
)
from agenten.agent_factory.skill_evaluation import (
    HermesSkillEvaluationEvidence,
    HermesSkillEvaluationRequest,
)
from agenten.agent_factory.skill_store import (
    InMemorySkillEvaluationRepository,
    SkillEvaluationStore,
)
from tests.agent_factory.test_candidate_evaluation import _write_candidate_archive
from tests.agent_factory.test_skill_evaluation_contracts import (
    NOW,
    evidence_payload,
    gap_payload,
    request_payload,
)


@dataclass
class Clock:
    current: datetime

    def now(self) -> datetime:
        return self.current


class Hermes:
    def __init__(self, evidence: list[HermesSkillEvaluationEvidence]) -> None:
        self._evidence = list(evidence)
        self.calls = 0

    async def evaluate_skill(
        self,
        request: HermesSkillEvaluationRequest,
    ) -> HermesSkillEvaluationEvidence:
        self.calls += 1
        return self._evidence.pop(0)


class CandidateStore:
    def __init__(self, candidates: list[ResolvedFactoryCandidate]) -> None:
        self._candidates = list(candidates)
        self.calls = 0

    def candidate_for_evaluation(
        self,
        request: HermesSkillEvaluationRequest,
        evidence: HermesSkillEvaluationEvidence,
    ) -> ResolvedFactoryCandidate:
        self.calls += 1
        return self._candidates.pop(0)


def _candidate(
    tmp_path: Path,
    *,
    build_fails: bool = False,
    test_fails: bool = False,
) -> tuple[FactoryCandidateManifest, Path]:
    archive_path = tmp_path / "candidate.zip"
    team_ref, workflow_ref, input_schema_ref, output_schema_ref, source_ref = _write_candidate_archive(archive_path)
    return (
        FactoryCandidateManifest(
            candidate_id="support_triage_v1",
            source_archive_ref=source_ref,
            team_manifest={"reference": team_ref, "relative_path": "team_manifest.json"},
            workflow_artifacts=({"reference": workflow_ref, "relative_path": "workflows/support_triage.json"},),
            tool_schema_artifacts=(
                {"reference": input_schema_ref, "relative_path": "schemas/support_triage.input.json"},
                {"reference": output_schema_ref, "relative_path": "schemas/support_triage.output.json"},
            ),
            n8n_tools=(
                TypedN8nTool(
                    name="support_triage",
                    description="Route a support request.",
                    input_schema_ref=input_schema_ref.uri,
                    output_schema_ref=output_schema_ref.uri,
                ),
            ),
            build_command=(
                ("python", "-c", "raise SystemExit(1)")
                if build_fails
                else ("python", "-m", "compileall", "-q", ".")
            ),
            real_case_command=(
                ("python", "-c", "raise SystemExit(1)")
                if test_fails
                else ("python", "run_case.py")
            ),
            timeout_seconds=10,
        ),
        archive_path,
    )


def _request(candidate: FactoryCandidateManifest, *, max_iterations: int = 3) -> HermesSkillEvaluationRequest:
    return HermesSkillEvaluationRequest.model_validate(
        request_payload(
            candidate_source_ref=candidate.source_archive_ref.model_dump(mode="json"),
            max_iterations=max_iterations,
        )
    )


def _evidence(
    request: HermesSkillEvaluationRequest,
    *,
    attempt: int = 1,
    required_gap: bool = False,
    proposed_test_fails: bool = False,
) -> HermesSkillEvaluationEvidence:
    checks = evidence_payload()["checks"]
    if proposed_test_fails:
        checks = [checks[0], {**checks[1], "status": "failed"}]
    payload = evidence_payload(
        evidence_id=str(UUID(int=0x105 + attempt)),
        request=request.model_dump(mode="json", by_alias=True),
        request_id=str(request.request_id),
        job_id=str(request.job_id),
        correlation_id=str(request.correlation_id),
        subject_id=request.subject_id,
        subject_version=request.subject_version,
        tool_gaps=(
            [gap_payload(severity="required", status="unresolved")]
            if required_gap
            else []
        ),
        checks=checks,
        outcome="redo" if proposed_test_fails else "passed",
    )
    payload["receipt"] = {
        **payload["receipt"],
        "receipt_id": str(UUID(int=0x104 + attempt)),
        "request_id": str(request.request_id),
        "job_id": str(request.job_id),
        "correlation_id": str(request.correlation_id),
        "lease_id": request.lease.lease_id,
        "released_skill": request.released_skill.model_dump(mode="json", by_alias=True),
        "used_skill_id": request.released_skill.skill_id,
        "used_skill_version": request.released_skill.version,
        "used_skill_sha256": request.released_skill.content_sha256,
    }
    payload["candidate"] = {
        **payload["candidate"],
        "candidate_id": f"factory_skill_evaluator_candidate_{attempt}",
        "request_id": str(request.request_id),
        "parent_released_skill": request.released_skill.model_dump(mode="json", by_alias=True),
    }
    return HermesSkillEvaluationEvidence.model_validate(payload)


def _coordinator(
    tmp_path: Path,
    *,
    hermes: Hermes,
    candidates: CandidateStore,
    clock: Clock | None = None,
) -> tuple[HermesSkillEvaluationCoordinator, SkillEvaluationStore]:
    store = SkillEvaluationStore(
        repository=InMemorySkillEvaluationRepository(),
        evidence_store=FilesystemSkillEvaluationEvidenceStore(tmp_path / "evidence"),
    )
    coordinator = HermesSkillEvaluationCoordinator(
        cli=hermes,
        evaluator=FactoryCandidateEvaluator(),
        candidate_store=candidates,
        private_store=store,
        clock=clock or Clock(NOW + timedelta(seconds=30)),
    )
    return coordinator, store


@pytest.mark.asyncio
async def test_successful_skill_usage_retains_private_candidate(tmp_path: Path) -> None:
    candidate, archive = _candidate(tmp_path)
    request = _request(candidate)
    hermes = Hermes([_evidence(request)])
    candidates = CandidateStore([ResolvedFactoryCandidate(candidate=candidate, source_archive=archive)])
    coordinator, store = _coordinator(tmp_path, hermes=hermes, candidates=candidates)

    result = await coordinator.evaluate(request)

    assert result.evidence.outcome == "passed"
    assert result.candidate_ref is not None
    assert result.iterations == 1
    assert store.get_evaluation(result.evidence.evidence_id).candidate_ref == result.candidate_ref


@pytest.mark.asyncio
async def test_sealed_evaluator_reconciles_untrusted_hermes_check_statuses(tmp_path: Path) -> None:
    candidate, archive = _candidate(tmp_path)
    request = _request(candidate)
    hermes = Hermes([_evidence(request, proposed_test_fails=True)])
    candidates = CandidateStore([ResolvedFactoryCandidate(candidate=candidate, source_archive=archive)])
    coordinator, _ = _coordinator(tmp_path, hermes=hermes, candidates=candidates)

    result = await coordinator.evaluate(request)

    assert result.evidence.outcome == "passed"
    assert all(check.status == "passed" for check in result.evidence.checks)
    assert result.candidate_ref is not None


@pytest.mark.asyncio
async def test_build_failure_records_evidence_without_retaining_candidate(tmp_path: Path) -> None:
    candidate, archive = _candidate(tmp_path, build_fails=True)
    request = _request(candidate)
    hermes = Hermes([_evidence(request)])
    candidates = CandidateStore([ResolvedFactoryCandidate(candidate=candidate, source_archive=archive)])
    coordinator, store = _coordinator(tmp_path, hermes=hermes, candidates=candidates)

    result = await coordinator.evaluate(request)

    assert result.evidence.outcome == "failed"
    assert result.candidate_ref is None
    assert store.get_evaluation(result.evidence.evidence_id).candidate_ref is None
    assert hermes.calls == 1


@pytest.mark.asyncio
async def test_test_failure_uses_the_bounded_improvement_iteration(tmp_path: Path) -> None:
    failing, archive = _candidate(tmp_path, test_fails=True)
    succeeding = failing.model_copy(update={"real_case_command": ("python", "run_case.py")})
    request = _request(failing, max_iterations=2)
    hermes = Hermes([_evidence(request, attempt=1), _evidence(request, attempt=2)])
    candidates = CandidateStore(
        [
            ResolvedFactoryCandidate(candidate=failing, source_archive=archive),
            ResolvedFactoryCandidate(candidate=succeeding, source_archive=archive),
        ]
    )
    coordinator, _ = _coordinator(tmp_path, hermes=hermes, candidates=candidates)

    result = await coordinator.evaluate(request)

    assert result.evidence.outcome == "passed"
    assert result.iterations == 2
    assert hermes.calls == 2
    assert candidates.calls == 2


@pytest.mark.asyncio
async def test_stale_lease_is_rejected_before_hermes_or_candidate_access(tmp_path: Path) -> None:
    candidate, archive = _candidate(tmp_path)
    request = _request(candidate)
    hermes = Hermes([_evidence(request)])
    candidates = CandidateStore([ResolvedFactoryCandidate(candidate=candidate, source_archive=archive)])
    coordinator, _ = _coordinator(
        tmp_path,
        hermes=hermes,
        candidates=candidates,
        clock=Clock(request.lease.expires_at),
    )

    with pytest.raises(FactoryDispatchError, match="active Factory lease"):
        await coordinator.evaluate(request)

    assert hermes.calls == 0
    assert candidates.calls == 0


@pytest.mark.asyncio
async def test_unresolved_required_gap_blocks_candidate_retention(tmp_path: Path) -> None:
    candidate, archive = _candidate(tmp_path)
    request = _request(candidate)
    hermes = Hermes([_evidence(request, required_gap=True)])
    candidates = CandidateStore([ResolvedFactoryCandidate(candidate=candidate, source_archive=archive)])
    coordinator, store = _coordinator(tmp_path, hermes=hermes, candidates=candidates)

    result = await coordinator.evaluate(request)

    assert result.evidence.outcome == "blocked_tool_gap"
    assert result.candidate_ref is None
    assert store.get_evaluation(result.evidence.evidence_id).candidate_ref is None

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
    HermesSkillUsageReceipt,
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


class SequenceClock:
    def __init__(self, values: list[datetime]) -> None:
        self._values = list(values)
        self._last = values[-1]

    def now(self) -> datetime:
        if self._values:
            self._last = self._values.pop(0)
        return self._last


class Hermes:
    def __init__(
        self,
        evidence: list[HermesSkillEvaluationEvidence],
        events: list[str] | None = None,
    ) -> None:
        self._evidence = list(evidence)
        self._receipts = [item.receipt for item in evidence]
        self.events = events if events is not None else []
        self.receipt_calls = 0
        self.calls = 0
        self.max_seconds: list[float] = []

    async def issue_skill_usage(
        self,
        request: HermesSkillEvaluationRequest,
        *,
        max_seconds: float,
    ) -> HermesSkillUsageReceipt:
        self.receipt_calls += 1
        self.max_seconds.append(max_seconds)
        self.events.append("receipt_issued")
        return self._receipts.pop(0)

    async def evaluate_skill(
        self,
        request: HermesSkillEvaluationRequest,
        *,
        receipt: HermesSkillUsageReceipt,
        candidate_result: object,
        candidate_id: str,
        candidate_source_ref: object,
        max_seconds: float,
    ) -> HermesSkillEvaluationEvidence:
        self.calls += 1
        self.max_seconds.append(max_seconds)
        self.events.append("proposal_requested")
        return self._evidence.pop(0)


class CandidateStore:
    def __init__(
        self,
        candidates: list[ResolvedFactoryCandidate],
        events: list[str] | None = None,
    ) -> None:
        self._candidates = list(candidates)
        self.events = events if events is not None else []
        self.calls = 0

    def candidate_for_evaluation(
        self,
        request: HermesSkillEvaluationRequest,
        receipt: HermesSkillUsageReceipt,
    ) -> ResolvedFactoryCandidate:
        self.calls += 1
        self.events.append("candidate_resolved")
        return self._candidates.pop(0)


class Evaluator:
    def __init__(self, events: list[str] | None = None) -> None:
        self._inner = FactoryCandidateEvaluator()
        self.events = events if events is not None else []
        self.calls = 0
        self.max_seconds: list[float] = []

    def evaluate_skill(self, *, max_seconds: float, **kwargs: object) -> object:
        self.calls += 1
        self.max_seconds.append(max_seconds)
        self.events.append("candidate_evaluated")
        return self._inner.evaluate_skill(max_seconds=max_seconds, **kwargs)


class RecordingStore:
    def __init__(self, inner: SkillEvaluationStore, events: list[str]) -> None:
        self._inner = inner
        self._events = events

    async def record_receipt(self, receipt: HermesSkillUsageReceipt):
        reference = await self._inner.record_receipt(receipt)
        self._events.append("receipt_persisted")
        return reference

    async def record_tool_gap(self, evaluation_id, marker):
        self._events.append("gap_persisted")
        return await self._inner.record_tool_gap(evaluation_id, marker)

    async def record_evaluation(self, evidence):
        self._events.append("evaluation_persisted")
        return await self._inner.record_evaluation(evidence)

    async def retain_candidate(self, evaluation_id, candidate):
        self._events.append("candidate_retained")
        return await self._inner.retain_candidate(evaluation_id, candidate)


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
        "outcome": "unresolved",
    }
    payload["candidate"] = {
        **payload["candidate"],
        "candidate_id": "support_triage_v1",
        "request_id": str(request.request_id),
        "content_ref": request.candidate_source_ref.model_dump(mode="json"),
        "content_sha256": request.candidate_source_ref.sha256,
        "parent_released_skill": request.released_skill.model_dump(mode="json", by_alias=True),
    }
    return HermesSkillEvaluationEvidence.model_validate(payload)


def _coordinator(
    tmp_path: Path,
    *,
    hermes: Hermes,
    candidates: CandidateStore,
    clock: Clock | SequenceClock | None = None,
    evaluator: Evaluator | None = None,
    events: list[str] | None = None,
) -> tuple[HermesSkillEvaluationCoordinator, SkillEvaluationStore]:
    store = SkillEvaluationStore(
        repository=InMemorySkillEvaluationRepository(),
        evidence_store=FilesystemSkillEvaluationEvidenceStore(tmp_path / "evidence"),
    )
    coordinator = HermesSkillEvaluationCoordinator(
        cli=hermes,
        evaluator=evaluator or Evaluator(events),
        candidate_store=candidates,
        private_store=store if events is None else RecordingStore(store, events),
        clock=clock or Clock(NOW + timedelta(minutes=1, seconds=30)),
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
async def test_usage_receipt_is_durable_before_candidate_resolution_or_evaluation(tmp_path: Path) -> None:
    events: list[str] = []
    candidate, archive = _candidate(tmp_path)
    request = _request(candidate)
    hermes = Hermes([_evidence(request)], events)
    candidates = CandidateStore(
        [ResolvedFactoryCandidate(candidate=candidate, source_archive=archive)],
        events,
    )
    evaluator = Evaluator(events)
    coordinator, _ = _coordinator(
        tmp_path,
        hermes=hermes,
        candidates=candidates,
        evaluator=evaluator,
        events=events,
    )

    await coordinator.evaluate(request)

    assert events.index("receipt_persisted") < events.index("candidate_resolved")
    assert events.index("receipt_persisted") < events.index("candidate_evaluated")
    assert events.index("candidate_evaluated") < events.index("proposal_requested")


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
async def test_sealed_evaluator_replaces_forged_hermes_checks_and_assertions(tmp_path: Path) -> None:
    candidate, archive = _candidate(tmp_path)
    request = _request(candidate)
    proposal = _evidence(request)
    forged_checks = tuple(
        check.model_copy(
            update={
                "check_id": f"forged-{check.check_id}",
                "evidence_ref": check.evidence_ref.model_copy(
                    update={"uri": f"artifact://forged/{check.check_id}"}
                ),
            }
        )
        for check in proposal.checks
    )
    proposal = proposal.model_copy(update={"checks": forged_checks})
    hermes = Hermes([proposal])
    candidates = CandidateStore([ResolvedFactoryCandidate(candidate=candidate, source_archive=archive)])
    coordinator, _ = _coordinator(tmp_path, hermes=hermes, candidates=candidates)

    result = await coordinator.evaluate(request)

    assert tuple(check.kind for check in result.evidence.checks) == ("build", "test")
    assert all(not check.check_id.startswith("forged-") for check in result.evidence.checks)
    assert all("artifact://forged/" not in check.evidence_ref.uri for check in result.evidence.checks)
    assert result.evidence.assertion_ids == request.acceptance_assertion_ids


@pytest.mark.asyncio
async def test_candidate_substitution_cannot_retain_an_unsealed_candidate(tmp_path: Path) -> None:
    candidate, archive = _candidate(tmp_path)
    request = _request(candidate)
    proposal = _evidence(request)
    substituted_ref = proposal.candidate.content_ref.model_copy(
        update={"uri": "artifact://factory/source/substituted", "sha256": "c" * 64}
    )
    substituted = proposal.candidate.model_copy(
        update={"content_ref": substituted_ref, "content_sha256": "c" * 64}
    )
    proposal = proposal.model_copy(update={"candidate": substituted})
    hermes = Hermes([proposal])
    candidates = CandidateStore([ResolvedFactoryCandidate(candidate=candidate, source_archive=archive)])
    coordinator, store = _coordinator(tmp_path, hermes=hermes, candidates=candidates)

    with pytest.raises(FactoryDispatchError, match="candidate.*sealed"):
        await coordinator.evaluate(request)

    assert store.get_evaluation(proposal.evidence_id) is None


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
    assert hermes.receipt_calls == 1
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
    assert hermes.receipt_calls == 2
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
    assert hermes.receipt_calls == 0
    assert candidates.calls == 0


@pytest.mark.asyncio
async def test_lease_expiry_between_receipt_and_evaluator_stops_incremental_work(tmp_path: Path) -> None:
    events: list[str] = []
    candidate, archive = _candidate(tmp_path)
    request = _request(candidate)
    active = request.lease.issued_at + timedelta(minutes=1)
    hermes = Hermes([_evidence(request)], events)
    candidates = CandidateStore(
        [ResolvedFactoryCandidate(candidate=candidate, source_archive=archive)],
        events,
    )
    evaluator = Evaluator(events)
    coordinator, _ = _coordinator(
        tmp_path,
        hermes=hermes,
        candidates=candidates,
        evaluator=evaluator,
        events=events,
        clock=SequenceClock([active, active, active, request.lease.expires_at]),
    )

    with pytest.raises(FactoryDispatchError, match="active Factory lease"):
        await coordinator.evaluate(request)

    assert "receipt_persisted" in events
    assert evaluator.calls == 0
    assert hermes.calls == 0


@pytest.mark.asyncio
async def test_remaining_lease_time_bounds_each_external_operation(tmp_path: Path) -> None:
    candidate, archive = _candidate(tmp_path)
    request = _request(candidate)
    now = request.lease.expires_at - timedelta(seconds=2)
    hermes = Hermes([_evidence(request)])
    candidates = CandidateStore([ResolvedFactoryCandidate(candidate=candidate, source_archive=archive)])
    evaluator = Evaluator()
    coordinator, _ = _coordinator(
        tmp_path,
        hermes=hermes,
        candidates=candidates,
        evaluator=evaluator,
        clock=Clock(now),
    )

    await coordinator.evaluate(request)

    assert hermes.max_seconds
    assert evaluator.max_seconds
    assert all(0 < seconds <= 2 for seconds in (*hermes.max_seconds, *evaluator.max_seconds))


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

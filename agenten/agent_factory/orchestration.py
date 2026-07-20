"""Ports that connect Captain's factory policy to Hermes and Minibook Forge."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from agenten.agent_factory.contracts import AgentFactoryJob, FactoryEvidenceBlock, FactoryLease, FactoryRole
from agenten.agent_factory.leases import FactoryLeasePort
from agenten.agent_factory.service import FactoryCoordinator
from agenten.agent_factory.skill_evaluation import (
    BoundedEvaluationCommand,
    HermesSkillCandidate,
    HermesSkillEvaluationEvidence,
    HermesSkillEvaluationRequest,
    HermesSkillUsageReceipt,
    SkillEvaluationCheck,
    ToolGapMarker,
    required_tool_gaps,
)
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_runtime.contracts import ArtifactRef

if TYPE_CHECKING:
    from agenten.agent_factory.candidate_evaluation import (
        FactoryCandidateEvaluationResult,
        FactoryCandidateManifest,
        ResolvedFactoryCandidate,
    )


class FactoryDispatchError(RuntimeError):
    """A provider cannot perform the Captain-authorized factory action."""


@dataclass(frozen=True)
class FactoryDispatch:
    job: AgentFactoryJob
    action: FactoryAction
    role: FactoryRole | None
    lease: FactoryLease | None


class FactoryClock(Protocol):
    def now(self) -> datetime: ...


class HermesFactoryPort(Protocol):
    """Execute one role step and return evidence through the gateway separately."""

    async def dispatch(self, request: FactoryDispatch) -> FactoryEvidenceBlock:
        """Run one leased Hermes role and return its untrusted evidence payload."""


class MinibookForgePort(Protocol):
    """Submit the approved build to Minibook's existing SwarmPipeline."""

    async def submit(self, request: FactoryDispatch) -> None:
        """Submit only; Forge must later post an immutable evidence block."""


class FactoryCandidateValidationPort(Protocol):
    """Evaluate the sealed generated candidate in an isolated workspace."""

    async def dispatch(self, request: FactoryDispatch) -> FactoryEvidenceBlock:
        """Return build, real-case, or quality evidence for the leased role."""


class HermesSkillEvaluationPort(Protocol):
    async def issue_skill_usage(
        self,
        request: HermesSkillEvaluationRequest,
        *,
        max_seconds: float,
    ) -> HermesSkillUsageReceipt: ...

    async def evaluate_skill(
        self,
        request: HermesSkillEvaluationRequest,
        *,
        receipt: HermesSkillUsageReceipt,
        candidate_result: "FactoryCandidateEvaluationResult",
        candidate_id: str,
        candidate_source_ref: ArtifactRef,
        max_seconds: float,
    ) -> HermesSkillEvaluationEvidence: ...


class SkillEvaluationCandidateStore(Protocol):
    def candidate_for_evaluation(
        self,
        request: HermesSkillEvaluationRequest,
        receipt: HermesSkillUsageReceipt,
    ) -> "ResolvedFactoryCandidate": ...


class SkillCandidateEvaluatorPort(Protocol):
    def evaluate_skill(
        self,
        *,
        request: HermesSkillEvaluationRequest,
        candidate: "FactoryCandidateManifest",
        source_archive: Path,
        max_seconds: float,
    ) -> "FactoryCandidateEvaluationResult": ...


class PrivateSkillEvaluationStore(Protocol):
    async def record_receipt(self, receipt: HermesSkillUsageReceipt) -> ArtifactRef: ...

    async def record_candidate_evaluation(
        self,
        request_id: UUID,
        result: "FactoryCandidateEvaluationResult",
    ) -> ArtifactRef: ...

    async def record_tool_gap(
        self,
        evaluation_id: UUID,
        marker: ToolGapMarker,
    ) -> ArtifactRef: ...

    async def record_evaluation(
        self,
        evidence: HermesSkillEvaluationEvidence,
    ) -> ArtifactRef: ...

    async def retain_candidate(
        self,
        evaluation_id: UUID,
        candidate: HermesSkillCandidate,
    ) -> ArtifactRef: ...


@dataclass(frozen=True)
class HermesSkillEvaluationResult:
    """Private evidence references ready for Captain/Gateway recording."""

    evidence: HermesSkillEvaluationEvidence
    evidence_ref: ArtifactRef
    candidate_ref: ArtifactRef | None
    iterations: int


class HermesSkillEvaluationCoordinator:
    """Evaluate one Captain-approved request without gaining release authority."""

    def __init__(
        self,
        *,
        cli: HermesSkillEvaluationPort,
        evaluator: SkillCandidateEvaluatorPort,
        candidate_store: SkillEvaluationCandidateStore,
        private_store: PrivateSkillEvaluationStore,
        clock: FactoryClock,
    ) -> None:
        self._cli = cli
        self._evaluator = evaluator
        self._candidate_store = candidate_store
        self._private_store = private_store
        self._clock = clock

    async def evaluate(
        self,
        request: HermesSkillEvaluationRequest,
    ) -> HermesSkillEvaluationResult:
        deadline = time.monotonic() + self._remaining_lease_seconds(request)
        for iteration in range(1, request.max_iterations + 1):
            receipt = await self._cli.issue_skill_usage(
                request,
                max_seconds=self._remaining_budget_seconds(request, deadline),
            )
            receipt_time = self._active_time(request)
            _require_staged_receipt(request, receipt, now=receipt_time)
            await self._private_store.record_receipt(receipt)
            self._remaining_budget_seconds(request, deadline)
            resolved = self._candidate_store.candidate_for_evaluation(request, receipt)
            evaluator_seconds = self._remaining_budget_seconds(request, deadline)
            try:
                candidate_result = self._evaluator.evaluate_skill(
                    request=request,
                    candidate=resolved.candidate,
                    source_archive=resolved.source_archive,
                    max_seconds=evaluator_seconds,
                )
            except (OSError, ValueError) as exc:
                raise FactoryDispatchError("sealed candidate evaluation could not start") from exc
            if candidate_result.candidate_manifest != resolved.candidate:
                raise FactoryDispatchError("sealed evaluator result does not match the resolved candidate manifest")
            self._remaining_budget_seconds(request, deadline)
            evaluator_ref = await self._private_store.record_candidate_evaluation(
                request.request_id,
                candidate_result,
            )
            self._remaining_budget_seconds(request, deadline)
            proposed = await self._cli.evaluate_skill(
                request,
                receipt=receipt,
                candidate_result=candidate_result,
                candidate_id=resolved.candidate.candidate_id,
                candidate_source_ref=resolved.candidate.source_archive_ref,
                max_seconds=self._remaining_budget_seconds(request, deadline),
            )
            if proposed.request != request or proposed.receipt != receipt:
                raise FactoryDispatchError("Hermes evaluation does not match the staged request and receipt")
            _require_sealed_candidate_binding(
                request,
                proposed,
                candidate_result.candidate_manifest,
            )
            check_time = self._active_time(request)
            evidence = _seal_candidate_result(
                proposed,
                candidate_result,
                evaluator_ref,
                check_time,
            )
            for marker in evidence.tool_gaps:
                self._remaining_budget_seconds(request, deadline)
                await self._private_store.record_tool_gap(evidence.evidence_id, marker)
            self._remaining_budget_seconds(request, deadline)
            evidence_ref = await self._private_store.record_evaluation(evidence)
            candidate_ref = None
            if (
                evidence.outcome == "passed"
                and evidence.candidate is not None
                and not required_tool_gaps(evidence)
            ):
                self._remaining_budget_seconds(request, deadline)
                candidate_ref = await self._private_store.retain_candidate(
                    evidence.evidence_id,
                    evidence.candidate,
                )
            result = HermesSkillEvaluationResult(
                evidence=evidence,
                evidence_ref=evidence_ref,
                candidate_ref=candidate_ref,
                iterations=iteration,
            )
            if _candidate_test_failed(candidate_result) and iteration < request.max_iterations:
                continue
            return result
        raise AssertionError("bounded skill evaluation loop did not return")

    def _remaining_lease_seconds(self, request: HermesSkillEvaluationRequest) -> float:
        now = self._active_time(request)
        remaining = (request.lease.expires_at - now).total_seconds()
        if remaining <= 0:
            raise FactoryDispatchError("skill evaluation requires an active Factory lease")
        return remaining

    def _remaining_budget_seconds(
        self,
        request: HermesSkillEvaluationRequest,
        deadline: float,
    ) -> float:
        monotonic_remaining = deadline - time.monotonic()
        if monotonic_remaining <= 0:
            raise FactoryDispatchError("skill evaluation timed out within the active lease")
        return min(self._remaining_lease_seconds(request), monotonic_remaining)

    def _active_time(self, request: HermesSkillEvaluationRequest) -> datetime:
        now = self._clock.now()
        if (
            now.tzinfo is None
            or now.utcoffset() != timezone.utc.utcoffset(now)
            or not request.lease.issued_at <= now < request.lease.expires_at
        ):
            raise FactoryDispatchError("skill evaluation requires an active Factory lease")
        return now


def _seal_candidate_result(
    proposed: HermesSkillEvaluationEvidence,
    candidate_result: "FactoryCandidateEvaluationResult",
    evaluator_ref: ArtifactRef,
    occurred_at: datetime,
) -> HermesSkillEvaluationEvidence:
    manifest = candidate_result.candidate_manifest
    if manifest is None:
        raise FactoryDispatchError("sealed evaluator result is missing its candidate manifest")
    required_gaps = required_tool_gaps(proposed)
    actual_checks = {check.name: check for check in candidate_result.checks}
    build_passed = (
        actual_checks.get("build") is not None
        and actual_checks["build"].status == "passed"
    )
    test_passed = (
        actual_checks.get("real_case") is not None
        and actual_checks["real_case"].status == "passed"
    )
    test_failed = (
        actual_checks.get("real_case") is not None
        and actual_checks["real_case"].status == "failed"
    )
    if candidate_result.status == "succeeded" and build_passed and test_passed:
        outcome = "blocked_tool_gap" if required_gaps else "passed"
        assertion_ids = candidate_result.assertion_ids
    elif build_passed and test_failed:
        outcome = "redo"
        assertion_ids = proposed.request.acceptance_assertion_ids
    else:
        outcome = "failed"
        assertion_ids = proposed.request.acceptance_assertion_ids
    build_status = "passed" if build_passed else "failed"
    test_status = "passed" if test_passed else "failed" if test_failed else "skipped"
    build_command = BoundedEvaluationCommand(
        command_id="captain.candidate.build",
        max_seconds=manifest.timeout_seconds,
    )
    test_command = BoundedEvaluationCommand(
        command_id="captain.candidate.real_case",
        max_seconds=manifest.timeout_seconds,
    )
    checks = (
        SkillEvaluationCheck(
            check_id="sealed-build",
            kind="build",
            command=build_command,
            status=build_status,
            occurred_at=occurred_at,
            evidence_ref=evaluator_ref,
            assertion_ids=(),
        ),
        SkillEvaluationCheck(
            check_id="sealed-test",
            kind="test",
            command=test_command,
            status=test_status,
            occurred_at=occurred_at,
            evidence_ref=evaluator_ref,
            assertion_ids=(
                candidate_result.assertion_ids if candidate_result.status == "succeeded" else ()
            ),
        ),
    )
    payload = proposed.model_dump(mode="python", by_alias=True)
    payload.update(
        {
            "checks": checks,
            "assertion_ids": assertion_ids,
            "outcome": outcome,
        }
    )
    return HermesSkillEvaluationEvidence.model_validate(payload)


def _require_staged_receipt(
    request: HermesSkillEvaluationRequest,
    receipt: HermesSkillUsageReceipt,
    *,
    now: datetime,
) -> None:
    if (
        receipt.request_id != request.request_id
        or receipt.job_id != request.job_id
        or receipt.correlation_id != request.correlation_id
        or receipt.lease_id != request.lease.lease_id
        or receipt.released_skill != request.released_skill
        or receipt.used_skill_id != request.released_skill.skill_id
        or receipt.used_skill_version != request.released_skill.version
        or receipt.used_skill_sha256 != request.released_skill.content_sha256
        or receipt.outcome != "unresolved"
        or receipt.occurred_at < request.occurred_at
        or not request.lease.issued_at <= receipt.occurred_at < request.lease.expires_at
        or receipt.occurred_at > now
    ):
        raise FactoryDispatchError("Hermes usage receipt does not match the Captain request")


def _require_sealed_candidate_binding(
    request: HermesSkillEvaluationRequest,
    proposed: HermesSkillEvaluationEvidence,
    candidate: "FactoryCandidateManifest",
) -> None:
    if candidate.source_archive_ref != request.candidate_source_ref:
        raise FactoryDispatchError("resolved candidate does not match the sealed Captain source")
    if proposed.candidate is None:
        return
    if (
        proposed.candidate.candidate_id != candidate.candidate_id
        or proposed.candidate.content_ref != candidate.source_archive_ref
        or proposed.candidate.content_sha256 != candidate.source_archive_ref.sha256
    ):
        raise FactoryDispatchError("Hermes candidate does not match the sealed candidate identity and digest")


def _candidate_test_failed(result: "FactoryCandidateEvaluationResult") -> bool:
    return any(check.name == "real_case" and check.status == "failed" for check in result.checks)


_ROLE_ACTIONS: dict[FactoryActionKind, FactoryRole] = {
    FactoryActionKind.DISPATCH_AGENT_ARCHITECT: FactoryRole.AGENT_ARCHITECT,
    FactoryActionKind.DISPATCH_TOOL_INTEGRATOR: FactoryRole.TOOL_INTEGRATOR,
    FactoryActionKind.DISPATCH_BUILD_VALIDATOR: FactoryRole.TOOL_INTEGRATOR,
    FactoryActionKind.DISPATCH_REAL_CASE_TESTER: FactoryRole.REAL_CASE_TESTER,
    FactoryActionKind.DISPATCH_QUALITY_WARDEN: FactoryRole.QUALITY_WARDEN,
}


class FactoryDispatcher:
    """Dispatch one allowed side effect; persistence remains in FactoryCoordinator."""

    def __init__(
        self,
        *,
        coordinator: FactoryCoordinator,
        hermes: HermesFactoryPort,
        forge: MinibookForgePort,
        candidate_validator: FactoryCandidateValidationPort | None = None,
        leases: FactoryLeasePort,
        clock: FactoryClock,
    ) -> None:
        self._coordinator = coordinator
        self._hermes = hermes
        self._forge = forge
        self._candidate_validator = candidate_validator
        self._leases = leases
        self._clock = clock

    async def dispatch_next(self, job_id: UUID) -> FactoryAction:
        action = self._coordinator.next_action(job_id)
        job = self._coordinator.projection(job_id).job
        if action.kind in _ROLE_ACTIONS:
            role = _ROLE_ACTIONS[action.kind]
            request = FactoryDispatch(
                job=job,
                action=action,
                role=role,
                lease=self._leases.active(job, role, action.attempt, self._clock.now()),
            )
            if action.kind is FactoryActionKind.DISPATCH_BUILD_VALIDATOR:
                if self._candidate_validator is None:
                    raise FactoryDispatchError("candidate build validator is not configured")
                evidence = await self._candidate_validator.dispatch(request)
            elif (
                action.kind
                in {
                    FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
                    FactoryActionKind.DISPATCH_QUALITY_WARDEN,
                }
                and self._candidate_validator is not None
            ):
                evidence = await self._candidate_validator.dispatch(request)
            else:
                evidence = await self._hermes.dispatch(request)
            self._coordinator.record(evidence)
            return action
        if action.kind is FactoryActionKind.SUBMIT_FORGE_JOB:
            await self._forge.submit(FactoryDispatch(job=job, action=action, role=None, lease=None))
            return action
        raise FactoryDispatchError(
            f"{action.kind.value} is a Captain state transition, not an external dispatch"
        )

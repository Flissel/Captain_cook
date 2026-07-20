"""Ports that connect Captain's factory policy to Hermes and Minibook Forge."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from agenten.agent_factory.contracts import AgentFactoryJob, FactoryEvidenceBlock, FactoryLease, FactoryRole
from agenten.agent_factory.leases import FactoryLeasePort
from agenten.agent_factory.service import FactoryCoordinator
from agenten.agent_factory.skill_evaluation import (
    HermesSkillCandidate,
    HermesSkillEvaluationEvidence,
    HermesSkillEvaluationRequest,
    HermesSkillUsageReceipt,
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
    async def evaluate_skill(
        self,
        request: HermesSkillEvaluationRequest,
    ) -> HermesSkillEvaluationEvidence: ...


class SkillEvaluationCandidateStore(Protocol):
    def candidate_for_evaluation(
        self,
        request: HermesSkillEvaluationRequest,
        evidence: HermesSkillEvaluationEvidence,
    ) -> "ResolvedFactoryCandidate": ...


class SkillCandidateEvaluatorPort(Protocol):
    def evaluate_skill(
        self,
        *,
        request: HermesSkillEvaluationRequest,
        candidate: "FactoryCandidateManifest",
        source_archive: Path,
    ) -> "FactoryCandidateEvaluationResult": ...


class PrivateSkillEvaluationStore(Protocol):
    async def record_receipt(self, receipt: HermesSkillUsageReceipt) -> ArtifactRef: ...

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
        now = self._clock.now()
        if (
            now.tzinfo is None
            or now.utcoffset() != timezone.utc.utcoffset(now)
            or not request.lease.issued_at <= now < request.lease.expires_at
        ):
            raise FactoryDispatchError("skill evaluation requires an active Factory lease")

        for iteration in range(1, request.max_iterations + 1):
            proposed = await self._cli.evaluate_skill(request)
            if proposed.request != request:
                raise FactoryDispatchError("Hermes evaluation does not match the Captain request")
            await self._private_store.record_receipt(proposed.receipt)
            resolved = self._candidate_store.candidate_for_evaluation(request, proposed)
            try:
                candidate_result = self._evaluator.evaluate_skill(
                    request=request,
                    candidate=resolved.candidate,
                    source_archive=resolved.source_archive,
                )
            except (OSError, ValueError) as exc:
                raise FactoryDispatchError("sealed candidate evaluation could not start") from exc

            evidence = _reconcile_candidate_result(proposed, candidate_result)
            for marker in evidence.tool_gaps:
                await self._private_store.record_tool_gap(evidence.evidence_id, marker)
            evidence_ref = await self._private_store.record_evaluation(evidence)
            candidate_ref = None
            if (
                evidence.outcome == "passed"
                and evidence.candidate is not None
                and not required_tool_gaps(evidence)
            ):
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


def _reconcile_candidate_result(
    proposed: HermesSkillEvaluationEvidence,
    candidate_result: "FactoryCandidateEvaluationResult",
) -> HermesSkillEvaluationEvidence:
    required_gaps = required_tool_gaps(proposed)
    if candidate_result.status == "succeeded":
        outcome = "blocked_tool_gap" if required_gaps else "passed"
        checks = tuple(
            check.model_copy(update={"status": "passed"}) for check in proposed.checks
        )
        assertion_ids = candidate_result.assertion_ids
    elif _candidate_test_failed(candidate_result):
        outcome = "redo"
        checks = tuple(
            check.model_copy(update={"status": "failed"})
            if check.kind == "test"
            else check.model_copy(update={"status": "passed"})
            for check in proposed.checks
        )
        assertion_ids = proposed.assertion_ids
    else:
        outcome = "failed"
        checks = tuple(
            check.model_copy(update={"status": "failed"})
            if check.kind == "build"
            else check.model_copy(update={"status": "skipped"})
            for check in proposed.checks
        )
        assertion_ids = proposed.assertion_ids
    payload = proposed.model_dump(mode="python", by_alias=True)
    payload.update(
        {
            "checks": checks,
            "assertion_ids": assertion_ids,
            "outcome": outcome,
        }
    )
    return HermesSkillEvaluationEvidence.model_validate(payload)


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
